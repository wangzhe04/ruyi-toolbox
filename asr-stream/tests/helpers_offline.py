"""131c 测试公用：假离线后端（整句识别）、WAV 打包、multipart 打包、带离线识别器起服务。纪律同 helpers.py：不载真模型。"""

from __future__ import annotations

import io
import threading
import wave

import numpy as np

from .helpers import SR


class FakeOffline:
    """「有声音的每 0.5 s 一个字」，语言固定 zh。"""

    name = "fake-offline"
    model_name = "fake-offline-model"

    def __init__(self, *, char: str = "句", fail=None):
        self.char = char
        self.fail = fail
        self.calls = 0

    def transcribe(self, samples: np.ndarray):
        self.calls += 1
        if self.fail:
            raise self.fail
        voiced = int(samples.size) if np.any(samples) else 0   # 正弦样例有过零点，别按非零样本数
        return self.char * (voiced // (SR // 2)), "zh"


def wav_bytes(pcm16: bytes, sr: int = SR, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16)
    return buf.getvalue()


def multipart_body(file_data: bytes, fields: dict | None = None, boundary: str = "----ruyi-test") -> tuple[bytes, str]:
    crlf = b"\r\n"
    b = boundary.encode()
    out = b""
    for k, v in (fields or {}).items():
        out += b"--" + b + crlf + ('Content-Disposition: form-data; name="%s"' % k).encode() + crlf + crlf + str(v).encode() + crlf
    out += (b"--" + b + crlf + b'Content-Disposition: form-data; name="file"; filename="voice.wav"' + crlf
            + b"Content-Type: audio/wav" + crlf + crlf + file_data + crlf)
    out += b"--" + b + b"--" + crlf
    return out, "multipart/form-data; boundary=" + boundary


def start_server_with_offline(manager, offline, port: int):
    from ruyi_asr_stream.engine import OfflineTranscriber
    from ruyi_asr_stream.server import Settings, build_server

    settings = Settings(["--port", str(port), "--model-dir", "x"])
    httpd = build_server(settings, manager, OfflineTranscriber(offline) if offline is not None else None)
    th = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    return httpd, th
