"""HTTP 层：安全闸、四条路由、分块限制、429、404、health。真起服务（127.0.0.1 随机端口），后端是假的。"""

from __future__ import annotations

import socket
import unittest

from ruyi_asr_stream import COMPONENT_NAME_TAG, __version__
from ruyi_asr_stream.engine import SessionManager
from ruyi_asr_stream.server import BIND_HOST, MAX_CHUNK_BYTES, pick_free_port

from .helpers import Client, FakeBackend, silence, speech, start_server


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = pick_free_port()
        cls.backend = FakeBackend()
        cls.mgr = SessionManager(cls.backend, max_sessions=2, idle_sec=30)
        cls.httpd, cls.thread = start_server(cls.mgr, cls.port)
        cls.c = Client(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.mgr.shutdown()

    def test_binds_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], BIND_HOST)
        lan_ip = socket.gethostbyname(socket.gethostname())
        if lan_ip.startswith("127."):
            # 很多 Linux（Debian/容器）把主机名解析到 127.0.x.1，那就没有「非回环地址」可以反证。
            self.skipTest(f"主机名解析到回环地址 {lan_ip}，无法反证只绑回环")
        with self.assertRaises(OSError):
            s = socket.create_connection((lan_ip, self.port), timeout=0.5)
            s.close()

    def test_health(self):
        st, j, _ = self.c.request("GET", "/health")
        self.assertEqual(st, 200)
        self.assertEqual(j["component"], COMPONENT_NAME_TAG)
        self.assertEqual(j["version"], __version__)
        self.assertTrue(j["ok"] and j["loaded"])
        self.assertEqual(j["sessions"], 0)
        self.assertEqual(j["backend"], "fake")

    def test_gate_rejects_origin_and_bad_host(self):
        st, j, _ = self.c.request("GET", "/health", headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        self.assertEqual(j["error"]["type"], "forbidden")
        st, _, _ = self.c.request("GET", "/health", headers={"Host": "evil.example:%d" % self.port})
        self.assertEqual(st, 403)
        st, _, _ = self.c.request("GET", "/health", headers={"Host": "localhost:%d" % self.port})
        self.assertEqual(st, 200)

    def test_full_session_flow(self):
        st, j, _ = self.c.open()
        self.assertEqual(st, 200)
        sid = j["id"]
        self.assertEqual(len(sid), 32)
        self.assertEqual(j["sampleRate"], 16000)
        st, j, _ = self.c.audio(sid, speech(1.0))
        self.assertEqual((st, j["partial"], j["finals"]), (200, "字字", []))
        st, j, _ = self.c.audio(sid, silence(0.8), ctype="application/octet-stream")
        self.assertEqual(st, 200)
        self.assertEqual([f["text"] for f in j["finals"]], ["字字"])
        st, j, _ = self.c.audio(sid, speech(0.5))
        self.assertEqual(j["partial"], "字")
        st, j, _ = self.c.finish(sid)
        self.assertEqual((st, [f["text"] for f in j["finals"]]), (200, ["字"]))
        st, j, _ = self.c.audio(sid, speech(0.1))
        self.assertEqual((st, j["error"]["type"]), (404, "unknown_session"))
        st, _, _ = self.c.request("GET", "/health")
        self.assertEqual(self.mgr.count(), 0)

    def test_delete(self):
        _, j, _ = self.c.open()
        st, _, data = self.c.delete(j["id"])
        self.assertEqual((st, data), (204, b""))
        st, _, _ = self.c.delete(j["id"])
        self.assertEqual(st, 404)
        st, _, _ = self.c.delete("0" * 32)
        self.assertEqual(st, 404)
        st, _, _ = self.c.request("DELETE", "/v1/stream/sessions/not-a-session-id")
        self.assertEqual(st, 404)

    def test_too_many_sessions(self):
        self.c.open()
        self.c.open()
        st, j, _ = self.c.open()
        self.assertEqual((st, j["error"]["type"]), (429, "too_many_sessions"))

    def test_hotwords_body(self):
        st, j, _ = self.c.open(["如意", "sherpa"])
        self.assertEqual(st, 200)
        self.assertEqual(self.backend.streams[-1].hotwords, "如意\nsherpa")
        st, j, _ = self.c.request("POST", "/v1/stream/sessions", b'{"hotwords": "x"}', {"Content-Type": "application/json"})
        self.assertEqual(st, 400)
        st, j, _ = self.c.request("POST", "/v1/stream/sessions", b"{not json", {"Content-Type": "application/json"})
        self.assertEqual(st, 400)

    def test_audio_validation(self):
        _, j, _ = self.c.open()
        sid = j["id"]
        st, j, _ = self.c.audio(sid, b"\x00")
        self.assertEqual((st, j["error"]["type"]), (400, "bad_request"))
        st, j, _ = self.c.audio(sid, speech(0.1), ctype="audio/wav")
        self.assertEqual(st, 415)
        st, j, _ = self.c.audio(sid, bytes(MAX_CHUNK_BYTES + 2))
        self.assertEqual(st, 413)
        st, j, _ = self.c.audio(sid, b"")
        self.assertEqual((st, j["partial"]), (200, ""))

    def test_unknown_routes_and_methods(self):
        st, _, _ = self.c.request("GET", "/v1/stream/sessions")
        self.assertEqual(st, 404)
        st, _, _ = self.c.request("POST", "/health", b"")
        self.assertEqual(st, 405)
        st, _, _ = self.c.request("PUT", "/health", b"")
        self.assertEqual(st, 405)
        st, _, _ = self.c.request("OPTIONS", "/v1/stream/sessions")
        self.assertEqual(st, 405)
        st, _, _ = self.c.request("GET", "/nope")
        self.assertEqual(st, 404)

    def test_decode_failure_is_500_not_crash(self):
        bad = SessionManager(FakeBackend(fail_on_decode=RuntimeError("boom")))
        port = pick_free_port()
        httpd, _ = start_server(bad, port)
        try:
            c = Client(port)
            _, j, _ = c.open()
            st, j, _ = c.audio(j["id"], speech(0.1))
            self.assertEqual((st, j["error"]["type"]), (500, "decode_failed"))
            st, _, _ = c.request("GET", "/health")
            self.assertEqual(st, 200)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
