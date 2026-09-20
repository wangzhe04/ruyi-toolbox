"""真 HTTP 往返的路由、安全闸、隐私判据（方案 §2.7 第二～四组）。

起的是真的 ThreadingHTTPServer，走的是真的 socket；只有引擎是假的。
"""

from __future__ import annotations

import glob
import http.client
import io
import json
import logging
import os
import tempfile
import threading
import unittest

from ruyi_asr_shim import COMPONENT_NAME_TAG, __version__
from ruyi_asr_shim.audio import TEMP_PREFIX
from ruyi_asr_shim.engine import EngineManager
from ruyi_asr_shim.server import BIND_HOST, MAX_UPLOAD_BYTES, Settings, build_server, pick_free_port

from .helpers import FakeBackend, Recorder, build_multipart, make_wav


class ServerFixture:
    def __init__(self, factory=None, **backend_kw):
        self.rec = Recorder()
        self.port = pick_free_port()
        self.settings = Settings(["--port", str(self.port), "--idle-unload-sec", "0"])
        make = factory or (lambda: FakeBackend(self.rec, **backend_kw))
        self.manager = EngineManager(make, idle_unload_sec=0)
        self.httpd = build_server(self.settings, self.manager)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        self.manager.shutdown()

    # ── 打一发请求 ────────────────────────────────────────────────────────

    def request(self, method, path, *, body=None, headers=None, host=None, send_body=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            hdrs = dict(headers or {})
            if host is not None:
                hdrs["Host"] = host
            if body is not None and not send_body:
                conn.putrequest(method, path, skip_host="Host" in hdrs, skip_accept_encoding=True)
                for k, v in hdrs.items():
                    conn.putheader(k, v)
                conn.endheaders()  # 故意不发体：只验 Content-Length 预检
            else:
                conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    def json_request(self, method, path, **kw):
        status, headers, data = self.request(method, path, **kw)
        try:
            return status, headers, json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return status, headers, {"_raw": data[:200]}

    def transcribe(self, *, audio=None, fields=None, filename="clip.wav", ctype="audio/wav",
                   headers=None, include_file=True):
        f = dict({"model": "qwen3-asr-0.6b", "response_format": "json"}, **(fields or {}))
        files = [("file", filename, ctype, audio if audio is not None else make_wav(0.3))] if include_file else []
        content_type, body = build_multipart(f, files)
        h = dict({"Content-Type": content_type}, **(headers or {}))
        return self.json_request("POST", "/v1/audio/transcriptions", body=body, headers=h)


class TestRoutes(unittest.TestCase):
    def test_health_does_not_load_model(self):
        with ServerFixture() as s:
            for _ in range(3):
                status, _h, body = s.json_request("GET", "/health")
                self.assertEqual(status, 200)
            self.assertEqual(body["ok"], True)
            self.assertEqual(body["component"], COMPONENT_NAME_TAG)
            self.assertEqual(body["version"], __version__)
            self.assertEqual(body["loaded"], False)
            self.assertEqual(body["model"], "qwen3-asr-0.6b")
            self.assertEqual(body["idleUnloadSec"], 0)
            self.assertIn("device", body)
            self.assertEqual(s.rec.loads, 0, "/health 绝不许触发模型加载")

    def test_health_trailing_slash(self):
        with ServerFixture() as s:
            self.assertEqual(s.json_request("GET", "/health/")[0], 200)

    def test_models(self):
        with ServerFixture() as s:
            status, _h, body = s.json_request("GET", "/v1/models")
            self.assertEqual(status, 200)
            self.assertEqual(body["object"], "list")
            self.assertEqual([m["id"] for m in body["data"]], ["qwen3-asr-0.6b"])
            self.assertEqual(s.rec.loads, 0)

    def test_unknown_path_404(self):
        with ServerFixture() as s:
            status, _h, body = s.json_request("GET", "/nope")
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["type"], "not_found")

    def test_chat_completions_is_404(self):
        """本 shim 明确不做 chat-audio 形；打过来要回 404 而不是装死。"""
        with ServerFixture() as s:
            status, _h, body = s.json_request("POST", "/v1/chat/completions",
                                              body=b"{}", headers={"Content-Type": "application/json"})
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["type"], "not_found")

    def test_method_not_allowed(self):
        with ServerFixture() as s:
            self.assertEqual(s.json_request("GET", "/v1/audio/transcriptions")[0], 405)
            self.assertEqual(s.json_request("POST", "/health", body=b"")[0], 405)
            self.assertEqual(s.json_request("OPTIONS", "/v1/models")[0], 405)

    def test_no_cors_headers(self):
        with ServerFixture() as s:
            _status, headers, _body = s.json_request("GET", "/health")
            for k in headers:
                self.assertFalse(k.lower().startswith("access-control-"),
                                 "不许回任何 CORS 头，回了就等于欢迎网页来打本机端口")


class TestSecurityGates(unittest.TestCase):
    def test_bind_address_is_loopback(self):
        with ServerFixture() as s:
            self.assertEqual(BIND_HOST, "127.0.0.1")
            self.assertEqual(s.httpd.server_address[0], "127.0.0.1",
                             "只许监听 127.0.0.1，不提供改绑 0.0.0.0 的开关")

    def test_bad_host_header_rejected(self):
        with ServerFixture() as s:
            for bad in ("evil.example", "evil.example:%d" % s.port, "127.0.0.1:1",
                        "attacker.local", "0.0.0.0:%d" % s.port):
                status, _h, body = s.json_request("GET", "/health", host=bad)
                self.assertEqual(status, 403, "Host=%s 应当 403（防 DNS rebinding）" % bad)
                self.assertEqual(body["error"]["type"], "forbidden")

    def test_good_host_headers_accepted(self):
        with ServerFixture() as s:
            for good in ("127.0.0.1:%d" % s.port, "localhost:%d" % s.port,
                         "LOCALHOST:%d" % s.port):
                self.assertEqual(s.json_request("GET", "/health", host=good)[0], 200)

    def test_origin_header_rejected(self):
        with ServerFixture() as s:
            for origin in ("http://evil.example", "null", "http://127.0.0.1:%d" % s.port):
                status, _h, body = s.json_request("GET", "/health", headers={"Origin": origin})
                self.assertEqual(status, 403, "带 Origin 的一律拒（如意用 Node fetch 出站，不带 Origin）")
                self.assertEqual(body["error"]["type"], "forbidden")

    def test_origin_gate_applies_to_transcribe(self):
        with ServerFixture() as s:
            status, _h, _b = s.transcribe(headers={"Origin": "http://evil.example"})
            self.assertEqual(status, 403)
            self.assertEqual(s.rec.loads, 0)


class TestTranscribe(unittest.TestCase):
    def test_happy_path(self):
        with ServerFixture(text="你好，今天下午三点开会。", language="Chinese") as s:
            status, headers, body = s.transcribe()
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
            # 主仓那段解析代码只认这两个：顶层字符串 text，可选 language。
            self.assertIsInstance(body["text"], str)
            self.assertEqual(body["text"], "你好，今天下午三点开会。")
            self.assertEqual(body["language"], "Chinese")
            self.assertEqual(s.rec.loads, 1)
            self.assertEqual(s.rec.calls, 1)

    def test_empty_text_is_ok(self):
        with ServerFixture(text="", language="") as s:
            status, _h, body = s.transcribe()
            self.assertEqual(status, 200)
            self.assertEqual(body["text"], "")
            self.assertNotIn("language", body)

    def test_arbitrary_model_name_accepted(self):
        """请求里的 model 只记录、不拒收 —— 如意填什么都能转。"""
        with ServerFixture() as s:
            for name in ("whisper-1", "随便写的", "", "x" * 300):
                status, _h, body = s.transcribe(fields={"model": name})
                self.assertEqual(status, 200, "model=%r 不该被拒" % name[:20])
                self.assertIsInstance(body["text"], str)

    def test_language_and_prompt_reach_backend(self):
        seen = {}

        class Spy(FakeBackend):
            def transcribe(self, pcm, language, prompt):
                seen["language"] = language
                seen["prompt"] = prompt
                seen["seconds"] = pcm.duration_sec
                return {"text": "ok", "language": "Chinese"}

        rec = Recorder()
        with ServerFixture(factory=lambda: Spy(rec)) as s:
            status, _h, _b = s.transcribe(
                fields={"language": "zh", "prompt": "如意 工作台"}, audio=make_wav(0.5)
            )
            self.assertEqual(status, 200)
            self.assertEqual(seen["language"], "zh")
            self.assertEqual(seen["prompt"], "如意 工作台")
            self.assertAlmostEqual(seen["seconds"], 0.5, places=2)

    def test_response_format_text(self):
        with ServerFixture(text="纯文本") as s:
            status, headers, data = s.request(
                "POST", "/v1/audio/transcriptions",
                **_multipart_kw({"model": "m", "response_format": "text"})
            )
            self.assertEqual(status, 200)
            self.assertTrue(headers["Content-Type"].startswith("text/plain"))
            self.assertEqual(data.decode("utf-8"), "纯文本")

    def test_verbose_json(self):
        with ServerFixture(text="hi") as s:
            status, _h, body = s.transcribe(fields={"response_format": "verbose_json"})
            self.assertEqual(status, 200)
            self.assertEqual(body["text"], "hi")
            self.assertEqual(body["task"], "transcribe")
            self.assertGreater(body["duration"], 0)

    def test_bad_response_format(self):
        with ServerFixture() as s:
            status, _h, body = s.transcribe(fields={"response_format": "srt"})
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["type"], "bad_request")

    def test_missing_file_field(self):
        with ServerFixture() as s:
            status, _h, body = s.transcribe(include_file=False)
            self.assertEqual(status, 400)
            self.assertIn("file", body["error"]["message"])
            self.assertEqual(s.rec.loads, 0)

    def test_empty_file(self):
        with ServerFixture() as s:
            status, _h, body = s.transcribe(audio=b"")
            self.assertEqual(status, 400)
            self.assertEqual(s.rec.loads, 0)

    def test_non_multipart_body(self):
        with ServerFixture() as s:
            status, _h, body = s.json_request(
                "POST", "/v1/audio/transcriptions",
                body=json.dumps({"model": "x"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["type"], "bad_request")

    def test_malformed_multipart(self):
        with ServerFixture() as s:
            status, _h, body = s.json_request(
                "POST", "/v1/audio/transcriptions",
                body=b"not really a multipart body",
                headers={"Content-Type": "multipart/form-data; boundary=BB"},
            )
            self.assertEqual(status, 400)

    def test_over_25mb_rejected(self):
        with ServerFixture() as s:
            status, _h, body = s.request(
                "POST", "/v1/audio/transcriptions",
                body=b"x",
                send_body=False,
                headers={
                    "Content-Type": "multipart/form-data; boundary=BB",
                    "Content-Length": str(MAX_UPLOAD_BYTES + 1),
                },
            )
            self.assertEqual(status, 413)
            self.assertEqual(json.loads(body)["error"]["type"], "payload_too_large")
            self.assertEqual(s.rec.loads, 0, "超限的体不该惊动模型")

    def test_just_under_limit_is_read(self):
        """25 MB 是【上限】不是禁区：刚好在线内的体要真的读进来（这里用一份小的证明路径通）。"""
        with ServerFixture() as s:
            big = make_wav(2.0)
            status, _h, body = s.transcribe(audio=big)
            self.assertEqual(status, 200)

    def test_unsupported_format_415(self):
        with ServerFixture() as s:
            webm = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
            status, _h, body = s.transcribe(audio=webm, filename="a.webm", ctype="audio/webm")
            self.assertEqual(status, 415)
            self.assertEqual(body["error"]["type"], "unsupported_media_type")
            self.assertIn("wav", body["error"]["message"])
            self.assertEqual(s.rec.loads, 0)

    def test_corrupt_audio_400(self):
        with ServerFixture() as s:
            broken = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"garbage" * 4
            status, _h, body = s.transcribe(audio=broken)
            self.assertIn(status, (400, 415))

    def test_engine_error_is_human_envelope_500(self):
        with ServerFixture(raise_on_call=RuntimeError("CUDA out of memory")) as s:
            status, _h, body = s.transcribe()
            self.assertEqual(status, 500)
            self.assertEqual(body["error"]["type"], "engine_error")
            self.assertIn("CUDA out of memory", body["error"]["message"])
            # 主仓把非 2xx 的回体前 1000 字直接给用户看，所以必须是 JSON 且是人话。
            self.assertLess(len(json.dumps(body)), 1000)

        # 出错之后服务还能继续干活（锁被放开了）
        with ServerFixture() as s:
            self.assertEqual(s.transcribe()[0], 200)

    def test_server_survives_and_serializes_concurrent_requests(self):
        with ServerFixture(call_sleep=0.1) as s:
            results = []

            def hit():
                results.append(s.transcribe()[0])

            threads = [threading.Thread(target=hit) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertEqual(results, [200] * 4)
            self.assertEqual(s.rec.max_concurrent, 1, "并发打进来也要串行推理")
            self.assertEqual(s.rec.loads, 1)


class TestPrivacy(unittest.TestCase):
    def test_no_temp_file_left_behind(self):
        pattern = os.path.join(tempfile.gettempdir(), TEMP_PREFIX + "*")
        before = set(glob.glob(pattern))
        with ServerFixture() as s:
            self.assertEqual(s.transcribe()[0], 200)
            self.assertEqual(s.transcribe(audio=make_wav(1.0))[0], 200)
        after = set(glob.glob(pattern))
        self.assertEqual(after - before, set(), "一趟请求之后临时目录不许有残留（音频不落盘）")

    def test_logs_carry_metadata_only(self):
        secret_text = "这句话是转写结果不许进日志"
        secret_name = "绝密会议录音.wav"
        logger = logging.getLogger("ruyi_asr_shim")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            with ServerFixture(text=secret_text) as s:
                self.assertEqual(s.transcribe(filename=secret_name)[0], 200)
                self.assertEqual(s.json_request("GET", "/health")[0], 200)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        logged = stream.getvalue()
        self.assertNotIn(secret_text, logged, "日志不得记转写文本")
        self.assertNotIn(secret_name, logged, "日志不得记文件名")
        self.assertIn("text_len=", logged, "但元数据要记下来，不然出了事查不到")
        self.assertIn("bytes=", logged)


def _multipart_kw(fields):
    ctype, body = build_multipart(fields, [("file", "clip.wav", "audio/wav", make_wav(0.3))])
    return {"body": body, "headers": {"Content-Type": ctype}}


if __name__ == "__main__":
    unittest.main()
