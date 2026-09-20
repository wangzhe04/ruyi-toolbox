"""引擎生命周期判据（方案 §2.7）：懒加载只一次、并发串行、空闲卸载后能重载、异常后锁要放开。

全程用假后端 —— 不加载真模型，没有 GPU 的机器也能跑。
"""

from __future__ import annotations

import threading
import time
import unittest

import numpy as np

from ruyi_asr_shim.audio import AudioBuffer
from ruyi_asr_shim.engine import EngineError, EngineManager

from .helpers import FakeBackend, Recorder


def pcm(seconds=0.5):
    return AudioBuffer(samples=np.zeros(int(16000 * seconds), dtype=np.float32), sample_rate=16000)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class TestLifecycle(unittest.TestCase):
    def test_not_loaded_until_first_request(self):
        rec = Recorder()
        m = EngineManager(lambda: FakeBackend(rec), idle_unload_sec=0)
        self.assertFalse(m.loaded)
        self.assertEqual(rec.loads, 0)
        self.assertEqual(m.status()["loaded"], False)
        self.assertEqual(rec.loads, 0, "只读 status() 不得触发加载")

        m.transcribe(pcm())
        self.assertTrue(m.loaded)
        self.assertEqual(rec.loads, 1)
        m.shutdown()

    def test_loads_only_once(self):
        rec = Recorder()
        m = EngineManager(lambda: FakeBackend(rec), idle_unload_sec=0)
        for _ in range(5):
            m.transcribe(pcm())
        self.assertEqual(rec.loads, 1)
        self.assertEqual(rec.calls, 5)
        m.shutdown()

    def test_concurrent_requests_are_serialized(self):
        rec = Recorder()
        m = EngineManager(lambda: FakeBackend(rec, call_sleep=0.15), idle_unload_sec=0)
        errs = []

        def run():
            try:
                m.transcribe(pcm())
            except Exception as exc:  # pragma: no cover
                errs.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errs, [])
        self.assertEqual(rec.calls, 4)
        self.assertEqual(rec.max_concurrent, 1, "推理必须串行（一把锁）")
        self.assertEqual(rec.loads, 1, "并发首发也只能加载一次")
        m.shutdown()

    def test_idle_unload_then_reload(self):
        rec = Recorder()
        clock = FakeClock()
        m = EngineManager(lambda: FakeBackend(rec), idle_unload_sec=600, clock=clock)
        m.transcribe(pcm())
        self.assertTrue(m.loaded)

        clock.advance(599)
        self.assertFalse(m.maybe_unload_idle(), "没到点不许卸")
        self.assertTrue(m.loaded)

        clock.advance(2)
        self.assertTrue(m.maybe_unload_idle(), "到点要卸")
        self.assertFalse(m.loaded)
        self.assertEqual(rec.unloads, 1)
        self.assertEqual(m.status()["device"], "unknown")

        m.transcribe(pcm())
        self.assertTrue(m.loaded)
        self.assertEqual(rec.loads, 2, "卸载之后下一发要重新加载")
        m.shutdown()

    def test_idle_zero_means_resident(self):
        rec = Recorder()
        clock = FakeClock()
        m = EngineManager(lambda: FakeBackend(rec), idle_unload_sec=0, clock=clock)
        m.transcribe(pcm())
        clock.advance(100000)
        self.assertFalse(m.maybe_unload_idle())
        self.assertTrue(m.loaded, "RUYI_ASR_IDLE_UNLOAD_SEC=0 表示常驻")
        m.shutdown()

    def test_idle_unload_thread_actually_fires(self):
        """不只是逻辑对：后台那条线程真的会把它卸掉（缩短阈值来测，与真机验收同一招）。"""
        rec = Recorder()
        m = EngineManager(lambda: FakeBackend(rec), idle_unload_sec=1)
        m.transcribe(pcm())
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and m.loaded:
            time.sleep(0.1)
        self.assertFalse(m.loaded, "空闲 1 秒之后后台线程应当已经卸载")
        self.assertEqual(rec.unloads, 1)
        m.shutdown()

    def test_unload_skipped_while_busy(self):
        """在途推理绝不能被空闲卸载打断。"""
        rec = Recorder()
        clock = FakeClock()
        m = EngineManager(lambda: FakeBackend(rec, call_sleep=0.4), idle_unload_sec=1, clock=clock)
        m.transcribe(pcm())
        clock.advance(100)

        started = threading.Event()
        holder = FakeBackend(rec, call_sleep=0.4)

        def slow():
            started.set()
            m.transcribe(pcm())

        th = threading.Thread(target=slow)
        th.start()
        started.wait(2)
        time.sleep(0.1)  # 让它确实进到锁里
        self.assertFalse(m.maybe_unload_idle(), "正在推理时不许卸载")
        th.join(10)
        self.assertTrue(m.loaded)
        m.shutdown()
        del holder


class TestErrors(unittest.TestCase):
    def test_inference_error_releases_lock(self):
        rec = Recorder()
        boom = {"on": True}

        def factory():
            return FakeBackend(rec, raise_on_call=RuntimeError("炸了") if boom["on"] else None)

        m = EngineManager(factory, idle_unload_sec=0)
        with self.assertRaises(EngineError) as ctx:
            m.transcribe(pcm())
        self.assertIn("炸了", str(ctx.exception))

        # 锁真的放开了：紧接着一发必须能拿到锁（拿不到就会在这里挂住、被 join 超时打出来）。
        done = threading.Event()

        def again():
            try:
                m.transcribe(pcm())
            except EngineError:
                pass
            done.set()

        th = threading.Thread(target=again, daemon=True)
        th.start()
        self.assertTrue(done.wait(5), "推理抛异常之后锁没被放开")
        m.shutdown()

    def test_load_error_is_engine_error_and_retryable(self):
        rec = Recorder()
        fail = {"on": True}

        def factory():
            return FakeBackend(rec, raise_on_load=OSError("模型目录不存在") if fail["on"] else None)

        m = EngineManager(factory, idle_unload_sec=0)
        with self.assertRaises(EngineError):
            m.transcribe(pcm())
        self.assertFalse(m.loaded)
        self.assertEqual(rec.loads, 0)

        fail["on"] = False
        m.transcribe(pcm())  # 修好之后不用重启服务
        self.assertTrue(m.loaded)
        m.shutdown()

    def test_bad_backend_shape_rejected(self):
        class Weird:
            device = "x"

            def load(self):
                pass

            def transcribe(self, *a, **k):
                return "不是 dict"

            def unload(self):
                pass

        m = EngineManager(Weird, idle_unload_sec=0)
        with self.assertRaises(EngineError):
            m.transcribe(pcm())
        m.shutdown()

    def test_shutdown_unloads(self):
        rec = Recorder()
        m = EngineManager(lambda: FakeBackend(rec), idle_unload_sec=0)
        m.transcribe(pcm())
        m.shutdown()
        self.assertEqual(rec.unloads, 1)
        self.assertFalse(m.loaded)


if __name__ == "__main__":
    unittest.main()
