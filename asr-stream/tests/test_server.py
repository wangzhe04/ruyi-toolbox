"""HTTP 层：安全闸、四条路由、分块限制、429、404、health。真起服务（127.0.0.1 随机端口），后端是假的。"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import unittest

from ruyi_asr_stream import COMPONENT_NAME_TAG, __version__
from ruyi_asr_stream.engine import SessionManager
from ruyi_asr_stream.server import BIND_HOST, MAX_CHUNK_BYTES, Settings, StreamHandler, build_server, pick_free_port

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

    def test_deeply_nested_json_is_400_not_dropped_connection(self):
        """修前：64 KB 以内的深嵌套 JSON 让 json.loads 抛 RecursionError（不是 ValueError），没接住、连接被断。"""
        for body in (b"[" * 50000, b'{"hotwords":' + b"[" * 30000 + b"]" * 30000 + b"}"):
            st, j, _ = self.c.request("POST", "/v1/stream/sessions", body, {"Content-Type": "application/json"})
            self.assertEqual((st, j["error"]["type"]), (400, "bad_request"))
        st, _, _ = self.c.request("GET", "/health")
        self.assertEqual(st, 200)

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


class SlowClientsAndLimitsTest(unittest.TestCase):
    """修前：handler 没有 socket 超时（timeout=None），声明 Content-Length 却不发完的慢客户端一直挂着一个线程；
    ThreadingHTTPServer 并发无上限；HEAD 经 _fail 写了 JSON 体，同一连接上下一个响应错位。"""

    def start(self, **kw):
        port = pick_free_port()
        httpd = build_server(Settings(["--port", str(port), "--model-dir", "x"]), SessionManager(FakeBackend()), **kw)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, port

    def slow_client(self, port):
        sk = socket.create_connection(("127.0.0.1", port), timeout=10)
        sk.sendall(("POST /v1/stream/sessions HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                    "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n" % port).encode() + b'{"hotwords"')
        return sk

    def test_handler_has_socket_timeout(self):
        self.assertEqual(StreamHandler.timeout, 30)

    def test_slow_client_is_dropped_after_timeout(self):
        httpd, port = self.start()
        httpd.RequestHandlerClass.timeout = 0.5   # 30 秒太久，测试里调短（只影响这个服务的绑定子类）
        sk = self.slow_client(port)
        try:
            t0 = time.monotonic()
            while sk.recv(4096):
                pass                                # 服务端超时后断开：recv 读到 b""
            self.assertLess(time.monotonic() - t0, 5.0)
        finally:
            sk.close()
        self.assertEqual(Client(port).request("GET", "/health")[0], 200)

    def test_too_many_in_flight_is_503_json(self):
        _httpd, port = self.start(max_inflight=1)
        c = Client(port)
        sk = self.slow_client(port)                 # 占住唯一的名额
        try:
            deadline = time.monotonic() + 5
            st, j, _ = c.request("GET", "/health")
            while st == 200 and time.monotonic() < deadline:   # 等慢客户端真的进了处理
                time.sleep(0.05)
                st, j, _ = c.request("GET", "/health")
            self.assertEqual((st, j["error"]["type"]), (503, "server_busy"))
        finally:
            sk.close()
        deadline = time.monotonic() + 5
        while c.request("GET", "/health")[0] != 200 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(c.request("GET", "/health")[0], 200, "名额要还回来")

    def test_head_sends_headers_only(self):
        _httpd, port = self.start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("HEAD", "/health")
            r = conn.getresponse()
            self.assertEqual(r.status, 405)
            r.read()
            conn.request("GET", "/health")             # 同一条连接：HEAD 若带了体，这一发就错位
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
