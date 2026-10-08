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



class TestModelSwitch(unittest.TestCase):
    """第 133 波:一个进程管多份模型 —— 同一时刻最多加载一份;请求点名另一份就先卸后载;unload_now 立刻释放。"""

    def make_factory(self, rec):
        made = []

        def factory(model_name=""):
            b = FakeBackend(rec, text="来自 " + (model_name or "缺省"))
            # auto 那份「实际挑中」的名字由后端报;这里模拟 auto → 0.6b
            b.resolved_model = "qwen3-asr-0.6b" if model_name in ("", "qwen3-asr-auto") else model_name
            made.append(model_name)
            return b

        return factory, made

    def test_same_model_does_not_reload(self):
        rec = Recorder()
        factory, made = self.make_factory(rec)
        m = EngineManager(factory, idle_unload_sec=0, default_model="qwen3-asr-auto")
        m.transcribe(pcm(), model="qwen3-asr-auto")
        m.transcribe(pcm())                       # 空 = 缺省(auto)
        m.transcribe(pcm(), model="qwen3-asr-0.6b")  # auto 实际挑的就是 0.6b → 不换
        self.assertEqual(rec.loads, 1)
        self.assertEqual(rec.unloads, 0)
        self.assertEqual(made, ["qwen3-asr-auto"])
        self.assertEqual(m.status()["model"], "qwen3-asr-0.6b")
        self.assertEqual(m.status()["requested"], "qwen3-asr-auto")
        m.shutdown()

    def test_switch_unloads_before_loading_other(self):
        rec = Recorder()
        factory, made = self.make_factory(rec)
        m = EngineManager(factory, idle_unload_sec=0, default_model="qwen3-asr-auto")
        self.assertEqual(m.transcribe(pcm())["text"], "来自 qwen3-asr-auto")
        self.assertEqual(m.transcribe(pcm(), model="qwen3-asr-1.7b")["text"], "来自 qwen3-asr-1.7b")
        self.assertEqual((rec.loads, rec.unloads), (2, 1), "先卸 0.6b 再载 1.7b:显存里最多一份")
        self.assertEqual(m.status()["model"], "qwen3-asr-1.7b")
        self.assertEqual(m.transcribe(pcm(), model="qwen3-asr-1.7b")["text"], "来自 qwen3-asr-1.7b")
        self.assertEqual(rec.loads, 2, "同一份不重载")
        self.assertEqual(m.transcribe(pcm(), model="qwen3-asr-auto")["text"], "来自 qwen3-asr-auto")
        self.assertEqual((rec.loads, rec.unloads), (3, 2))
        self.assertEqual(made, ["qwen3-asr-auto", "qwen3-asr-1.7b", "qwen3-asr-auto"])
        m.shutdown()

    def test_unload_now(self):
        rec = Recorder()
        factory, _made = self.make_factory(rec)
        m = EngineManager(factory, idle_unload_sec=0)
        self.assertFalse(m.unload_now(), "没加载回 False")
        m.transcribe(pcm())
        self.assertTrue(m.loaded)
        self.assertTrue(m.unload_now())
        self.assertFalse(m.loaded)
        self.assertEqual((rec.loads, rec.unloads), (1, 1))
        self.assertEqual(m.status()["device"], "unknown")
        m.transcribe(pcm())
        self.assertEqual(rec.loads, 2, "卸了还能再载")
        m.shutdown()

    def test_unload_now_waits_for_inflight(self):
        rec = Recorder()
        m = EngineManager(lambda: FakeBackend(rec, call_sleep=0.3), idle_unload_sec=0)
        m.transcribe(pcm())
        results = {}

        def worker():
            results["r"] = m.transcribe(pcm())

        th = threading.Thread(target=worker)
        th.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        self.assertTrue(m.unload_now())
        self.assertGreaterEqual(time.monotonic() - t0, 0.2, "在途那一发转完才卸")
        th.join(5)
        self.assertEqual(results["r"]["text"], "你好世界", "在途请求不受影响")
        self.assertEqual(rec.unloads, 1)
        m.shutdown()

    def test_old_style_factory_still_works(self):
        rec = Recorder()
        m = EngineManager(lambda: FakeBackend(rec), idle_unload_sec=0)
        m.transcribe(pcm(), model="whatever")
        m.transcribe(pcm(), model="other")
        self.assertEqual(rec.loads, 2, "无参工厂造不出别的尺寸,但换名字照样走「先卸后载」的路径")
        m.shutdown()


class SlowLoad(FakeBackend):
    """加载卡在 release 上（模拟首次在线下载／冷盘加载要好几分钟）；开始加载时 started 置位。"""

    def __init__(self, rec, started, release):
        super().__init__(rec)
        self.started = started
        self.release = release

    def load(self):
        self.started.set()
        self.release.wait(10)
        with self.rec.lock:
            self.rec.loads += 1


class TestLoadingDoesNotBlockUnloadOrShutdown(unittest.TestCase):
    """修前：串行锁把模型加载（含首次在线下载）也包进去，unload_now 与 shutdown（看门狗退出走它）都要等加载完 ——
    如意被杀后孤儿进程一直活到加载结束、占着端口。"""

    def setUp(self):
        self.rec = Recorder()
        self.started = threading.Event()
        self.release = threading.Event()
        self.m = EngineManager(lambda: SlowLoad(self.rec, self.started, self.release), idle_unload_sec=0)
        self.out = {}

        def worker():
            try:
                self.out["r"] = self.m.transcribe(pcm())
            except Exception as exc:  # noqa: BLE001
                self.out["e"] = exc

        self.th = threading.Thread(target=worker, daemon=True)
        self.th.start()
        self.assertTrue(self.started.wait(5), "后端没开始加载")

    def tearDown(self):
        self.release.set()
        self.th.join(10)
        self.m.shutdown()

    def test_shutdown_does_not_wait_for_loading(self):
        self.assertTrue(self.m.status()["loading"])
        t0 = time.monotonic()
        self.m.shutdown()
        self.assertLess(time.monotonic() - t0, 1.0, "shutdown 不许排队等一次加载")
        self.release.set()
        self.th.join(5)
        self.assertIsInstance(self.out.get("e"), EngineError, "已经在退出：加载完也不再推理")
        self.assertEqual((self.rec.loads, self.rec.unloads), (1, 1), "加载完那一刻看到已关闭 → 立即卸载，不留显存")
        self.assertFalse(self.m.loaded)
        self.assertFalse(self.m.status()["loading"])
        with self.assertRaises(EngineError):
            self.m.transcribe(pcm())   # 关了就不再加载

    def test_unload_during_loading_returns_loading_state(self):
        t0 = time.monotonic()
        self.assertEqual(self.m.request_unload(), "loading")
        self.assertFalse(self.m.unload_now(), "正在加载：没卸成，回 False")
        self.assertLess(time.monotonic() - t0, 1.0, "unload 不许排队等一次加载")
        self.release.set()
        self.th.join(5)
        self.assertEqual(self.out.get("r", {}).get("text"), "你好世界", "在途那一发照常转完")
        self.assertEqual((self.rec.loads, self.rec.unloads), (1, 1), "转完就兑现那次卸载")
        self.assertFalse(self.m.loaded)
        self.assertEqual(self.m.request_unload(), "not_loaded")


if __name__ == "__main__":
    unittest.main()
