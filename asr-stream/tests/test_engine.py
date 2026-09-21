"""引擎层：会话生命周期、端点收口、finish、上限、空闲回收、热词清洗、PCM 解析。"""

from __future__ import annotations

import unittest

from ruyi_asr_stream.engine import SessionLimit, SessionManager, UnknownSession, _clean_hotwords, pcm16_to_float32

from .helpers import FakeBackend, silence, speech


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.backend = FakeBackend()
        self.mgr = SessionManager(self.backend, max_sessions=2, idle_sec=30, clock=self.clock)

    def test_partial_then_endpoint_then_final(self):
        s = self.mgr.open()
        r1 = self.mgr.feed(s.id, speech(1.0))
        self.assertEqual(r1["partial"], "字字")
        self.assertEqual(r1["finals"], [])
        r2 = self.mgr.feed(s.id, speech(0.5))
        self.assertEqual(r2["partial"], "字字字")
        # 说过话之后静音 0.8 s → 收口成一句，partial 清空，时间轴从 0 到 2300 ms
        r3 = self.mgr.feed(s.id, silence(0.8))
        self.assertEqual(r3["partial"], "")
        self.assertEqual(len(r3["finals"]), 1)
        self.assertEqual(r3["finals"][0]["text"], "字字字")
        self.assertEqual(r3["finals"][0]["startMs"], 0)
        self.assertEqual(r3["finals"][0]["endMs"], 2300)
        # 下一句从新的起点算
        r4 = self.mgr.feed(s.id, speech(0.5))
        self.assertEqual(r4["partial"], "字")
        r5 = self.mgr.finish(s.id)
        self.assertEqual([f["text"] for f in r5["finals"]], ["字"])
        self.assertEqual(r5["finals"][0]["startMs"], 2300)
        self.assertEqual(self.mgr.count(), 0)
        with self.assertRaises(UnknownSession):
            self.mgr.feed(s.id, speech(0.1))

    def test_silence_only_never_finals(self):
        s = self.mgr.open()
        r = self.mgr.feed(s.id, silence(3.0))
        self.assertEqual(r, {"partial": "", "finals": []})
        self.assertEqual(self.mgr.finish(s.id), {"finals": []})

    def test_session_limit_and_delete(self):
        a = self.mgr.open()
        self.mgr.open()
        with self.assertRaises(SessionLimit):
            self.mgr.open()
        self.assertTrue(self.mgr.close(a.id))
        self.assertFalse(self.mgr.close(a.id))
        self.mgr.open()   # 腾出来了

    def test_idle_reap(self):
        s = self.mgr.open()
        self.mgr.feed(s.id, speech(0.2))
        self.clock.t += 31
        self.assertEqual(self.mgr.reap(), 1)
        with self.assertRaises(UnknownSession):
            self.mgr.get(s.id)

    def test_hotwords_reach_backend(self):
        s = self.mgr.open(["如意", " 工作台 ", "", "如意", "x" * 100, 123])
        self.assertEqual(self.backend.streams[-1].hotwords, "如意\n工作台\n" + "x" * 40)
        self.mgr.close(s.id)

    def test_clean_hotwords_caps(self):
        self.assertEqual(len(_clean_hotwords(["w%d" % i for i in range(500)])), 200)

    def test_pcm_parse(self):
        self.assertEqual(pcm16_to_float32(b"").size, 0)
        arr = pcm16_to_float32(b"\x00\x80\xff\x7f")
        self.assertAlmostEqual(float(arr[0]), -1.0)
        self.assertAlmostEqual(float(arr[1]), 32767 / 32768)
        with self.assertRaises(ValueError):
            pcm16_to_float32(b"\x00")

    def test_decode_error_propagates_and_session_survives(self):
        backend = FakeBackend(fail_on_decode=RuntimeError("boom"))
        mgr = SessionManager(backend)
        s = mgr.open()
        with self.assertRaises(RuntimeError):
            mgr.feed(s.id, speech(0.1))
        self.assertEqual(mgr.count(), 1)


if __name__ == "__main__":
    unittest.main()
