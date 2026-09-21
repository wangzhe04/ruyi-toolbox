"""131c：离线整句识别（SenseVoice）那条路 —— WAV 解码、/v1/audio/transcriptions、/v1/models、health、登记同时 provides asr；
131a：解码方式缺省 modified_beam_search。全部用假后端，不载真模型。"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ruyi_asr_stream import DEFAULT_OFFLINE_MODEL_NAME
from ruyi_asr_stream.audio import UnsupportedAudioError, decode_wav_to_mono16k
from ruyi_asr_stream.engine import OfflineTranscriber, SessionManager, normalize_decoding, resolve_offline_model_files
from ruyi_asr_stream.registry import DIR_ENV, build_record, register, registration_path, self_check
from ruyi_asr_stream.server import Settings, pick_free_port

from .helpers import Client, FakeBackend, speech, start_server
from .helpers_offline import FakeOffline, multipart_body, start_server_with_offline, wav_bytes


class DecodingDefaultTest(unittest.TestCase):
    def test_default_is_beam_search(self):
        self.assertEqual(normalize_decoding(""), "modified_beam_search")
        self.assertEqual(normalize_decoding("auto"), "modified_beam_search")
        self.assertEqual(normalize_decoding("greedy"), "greedy_search")
        self.assertEqual(normalize_decoding("GREEDY_SEARCH"), "greedy_search")
        self.assertEqual(normalize_decoding("nonsense"), "modified_beam_search")

    def test_settings_pick_up_flags(self):
        s = Settings(["--model-dir", "x"])
        self.assertEqual((s.decoding, s.offline_model_dir, s.offline_model_name), ("", "", DEFAULT_OFFLINE_MODEL_NAME))
        s = Settings(["--model-dir", "x", "--decoding", "greedy_search", "--offline-model-dir", "C:\\sv", "--offline-model", "sv-x"])
        self.assertEqual((s.decoding, s.offline_model_dir, s.offline_model_name), ("greedy_search", "C:\\sv", "sv-x"))


class WavDecodeTest(unittest.TestCase):
    def test_pcm16_mono_16k_passthrough(self):
        buf = decode_wav_to_mono16k(wav_bytes(speech(1.0)))
        self.assertEqual((buf.sample_rate, len(buf.samples)), (16000, 16000))
        self.assertAlmostEqual(buf.duration_sec, 1.0, places=3)

    def test_stereo_and_other_rates_are_folded(self):
        stereo = np.repeat(np.frombuffer(speech(1.0), "<i2"), 2).astype("<i2").tobytes()
        self.assertEqual(len(decode_wav_to_mono16k(wav_bytes(stereo, channels=2)).samples), 16000)
        self.assertEqual(len(decode_wav_to_mono16k(wav_bytes(speech(1.0)[:16000], sr=8000)).samples), 16000)

    def test_non_wav_rejected(self):
        with self.assertRaises(UnsupportedAudioError):
            decode_wav_to_mono16k(b"\x1aE\xdf\xa3webm-garbage")
        with self.assertRaises(UnsupportedAudioError):
            decode_wav_to_mono16k(b"RIFF\x00\x00\x00\x00WAVEjunk")


class OfflineTranscriberTest(unittest.TestCase):
    def test_transcribe_shape_and_count(self):
        t = OfflineTranscriber(FakeOffline())
        out = t.transcribe(np.frombuffer(speech(1.5), "<i2").astype(np.float32) / 32768.0)
        self.assertEqual(out, {"text": "句句句", "language": "zh"})
        self.assertEqual((t.count, t.model_name), (1, "fake-offline-model"))


class OfflineRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = pick_free_port()
        cls.offline = FakeOffline()
        cls.mgr = SessionManager(FakeBackend(), max_sessions=2, idle_sec=30)
        cls.httpd, _ = start_server_with_offline(cls.mgr, cls.offline, cls.port)
        cls.c = Client(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_health_and_models_mention_offline(self):
        st, j, _ = self.c.request("GET", "/health")
        self.assertEqual(st, 200)
        self.assertEqual((j["offline"]["model"], j["offline"]["backend"]), ("fake-offline-model", "fake-offline"))
        st, j, _ = self.c.request("GET", "/v1/models")
        self.assertEqual(st, 200)
        self.assertEqual([(m["id"], m["capabilities"]) for m in j["data"]],
                         [("zipformer-bilingual-zh-en", ["asr-stream"]), ("fake-offline-model", ["asr"])])
        st, _, _ = self.c.request("POST", "/v1/models", b"")
        self.assertEqual(st, 405)

    def test_transcriptions_json_text_verbose(self):
        body, ctype = multipart_body(wav_bytes(speech(1.0)), {"model": "whatever"})
        st, j, _ = self.c.request("POST", "/v1/audio/transcriptions", body, {"Content-Type": ctype})
        self.assertEqual((st, j), (200, {"text": "句句", "language": "zh"}))
        body, ctype = multipart_body(wav_bytes(speech(1.0)), {"response_format": "text"})
        st, _, data = self.c.request("POST", "/v1/audio/transcriptions", body, {"Content-Type": ctype})
        self.assertEqual((st, data.decode("utf-8")), (200, "句句"))
        body, ctype = multipart_body(wav_bytes(speech(0.5)), {"response_format": "verbose_json"})
        st, j, _ = self.c.request("POST", "/v1/audio/transcriptions", body, {"Content-Type": ctype})
        self.assertEqual((st, j["text"], j["task"], j["segments"]), (200, "句", "transcribe", []))
        self.assertAlmostEqual(j["duration"], 0.5, places=2)

    def test_transcriptions_rejections(self):
        st, j, _ = self.c.request("POST", "/v1/audio/transcriptions", b"{}", {"Content-Type": "application/json"})
        self.assertEqual((st, j["error"]["type"]), (415, "unsupported_media_type"))
        body, ctype = multipart_body(b"\x1aE\xdf\xa3not-a-wav")
        st, j, _ = self.c.request("POST", "/v1/audio/transcriptions", body, {"Content-Type": ctype})
        self.assertEqual((st, j["error"]["type"]), (415, "unsupported_media_type"))
        body, ctype = multipart_body(b"")
        st, j, _ = self.c.request("POST", "/v1/audio/transcriptions", body, {"Content-Type": ctype})
        self.assertEqual((st, j["error"]["type"]), (400, "bad_request"))
        body, ctype = multipart_body(wav_bytes(speech(0.2)), {"response_format": "srt"})
        st, j, _ = self.c.request("POST", "/v1/audio/transcriptions", body, {"Content-Type": ctype})
        self.assertEqual(st, 400)
        st, j, _ = self.c.request("POST", "/v1/audio/transcriptions", b"x", {"Content-Type": ctype, "Origin": "http://evil"})
        self.assertEqual(st, 403)

    def test_stream_route_still_works_alongside(self):
        st, j, _ = self.c.open()
        self.assertEqual(st, 200)
        st, j, _ = self.c.audio(j["id"], speech(0.5))
        self.assertEqual((st, j["partial"]), (200, "字"))

    def test_offline_failure_is_500_not_crash(self):
        port = pick_free_port()
        httpd, _ = start_server_with_offline(SessionManager(FakeBackend()), FakeOffline(fail=RuntimeError("boom")), port)
        try:
            c = Client(port)
            body, ctype = multipart_body(wav_bytes(speech(0.5)))
            st, j, _ = c.request("POST", "/v1/audio/transcriptions", body, {"Content-Type": ctype})
            self.assertEqual((st, j["error"]["type"]), (500, "decode_failed"))
            st, _, _ = c.request("GET", "/health")
            self.assertEqual(st, 200)
        finally:
            httpd.shutdown()
            httpd.server_close()


class NoOfflineTest(unittest.TestCase):
    def test_409_when_not_configured(self):
        port = pick_free_port()
        httpd, _ = start_server(SessionManager(FakeBackend()), port)
        try:
            c = Client(port)
            body, ctype = multipart_body(wav_bytes(speech(0.5)))
            st, j, _ = c.request("POST", "/v1/audio/transcriptions", body, {"Content-Type": ctype})
            self.assertEqual((st, j["error"]["type"]), (409, "offline_not_configured"))
            st, j, _ = c.request("GET", "/health")
            self.assertIsNone(j["offline"])
            st, j, _ = c.request("GET", "/v1/models")
            self.assertEqual(len(j["data"]), 1)
        finally:
            httpd.shutdown()
            httpd.server_close()


class OfflineRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asr-stream-off-")
        self.env = {DIR_ENV: os.path.join(self.tmp, "components")}
        self.pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.stream_dir = os.path.join(self.tmp, "stream")
        os.makedirs(self.stream_dir)
        for n in ("tokens.txt", "encoder-e.int8.onnx", "decoder-e.onnx", "joiner-e.int8.onnx"):
            Path(self.stream_dir, n).write_bytes(b"x")
        self.off_dir = os.path.join(self.tmp, "sv")
        os.makedirs(self.off_dir)
        for n in ("tokens.txt", "model.onnx", "model.int8.onnx"):
            Path(self.off_dir, n).write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolve_prefers_int8(self):
        self.assertTrue(resolve_offline_model_files(self.off_dir)["model"].endswith("model.int8.onnx"))
        os.unlink(os.path.join(self.off_dir, "model.int8.onnx"))
        self.assertTrue(resolve_offline_model_files(self.off_dir)["model"].endswith("model.onnx"))
        with self.assertRaises(FileNotFoundError):
            resolve_offline_model_files(os.path.join(self.tmp, "nope"))

    def test_record_provides_both(self):
        rec = build_record(python_exe="C:\\py.exe", cwd="C:\\x", model_dir="C:\\m", model_name="zipformer-bilingual-zh-en", port=8791,
                           offline_model_dir="C:\\sv")
        self.assertEqual(rec["provides"], [
            {"type": "asr-stream", "basePath": "/v1", "model": "zipformer-bilingual-zh-en"},
            {"type": "asr", "basePath": "/v1", "model": DEFAULT_OFFLINE_MODEL_NAME, "protocol": "transcriptions"},
        ])
        self.assertEqual(rec["run"]["env"]["RUYI_ASR_STREAM_OFFLINE_MODEL_DIR"], "C:\\sv")
        self.assertNotIn("RUYI_ASR_STREAM_OFFLINE_MODEL", rec["run"]["env"])
        rec2 = build_record(python_exe="C:\\py.exe", cwd="C:\\x", model_dir="C:\\m", model_name="zipformer-bilingual-zh-en", port=8791)
        self.assertEqual(len(rec2["provides"]), 1)
        self.assertNotIn("RUYI_ASR_STREAM_OFFLINE_MODEL_DIR", rec2["run"]["env"])

    def test_self_check_validates_offline_dir_when_given(self):
        self.assertTrue(self_check(sys.executable, self.pkg_root, self.stream_dir).ok)
        self.assertTrue(self_check(sys.executable, self.pkg_root, self.stream_dir, self.off_dir).ok)
        self.assertFalse(self_check(sys.executable, self.pkg_root, self.stream_dir, os.path.join(self.tmp, "nope")).ok)
        self.assertFalse(self_check(sys.executable, self.pkg_root, self.stream_dir, "relative/sv").ok)

    def test_register_with_offline(self):
        out = io.StringIO()
        self.assertEqual(register(model_dir=self.stream_dir, offline_model_dir=self.off_dir, env=self.env, out=out), 0)
        rec = json.loads(registration_path(self.env).read_text("utf-8"))
        self.assertEqual([p["type"] for p in rec["provides"]], ["asr-stream", "asr"])
        self.assertEqual(rec["run"]["env"]["RUYI_ASR_STREAM_OFFLINE_MODEL_DIR"], self.off_dir)
        self.assertEqual(register(model_dir=self.stream_dir, offline_model_dir=os.path.join(self.tmp, "nope"), env=self.env, out=io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
