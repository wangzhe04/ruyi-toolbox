"""测试公用：假后端、造 WAV、造 multipart、起一个真 HTTP 服务。

一条纪律：测试里【绝不】加载真模型、绝不联网、绝不碰 GPU。假后端把引擎的生命周期判据
（懒加载、串行、空闲卸载、异常后放锁）全部暴露成可断言的计数器。
"""

from __future__ import annotations

import io
import threading
import time
import wave

import numpy as np


class Recorder:
    """跨多次「加载」共享的计数器（EngineManager 每次加载都会新建一个后端实例）。"""

    def __init__(self):
        self.loads = 0
        self.unloads = 0
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.lock = threading.Lock()


class FakeBackend:
    device = "fake-device"

    def __init__(self, rec: Recorder, *, text="你好世界", language="Chinese",
                 load_sleep=0.0, call_sleep=0.0, raise_on_call=None, raise_on_load=None):
        self.rec = rec
        self.text = text
        self.language = language
        self.load_sleep = load_sleep
        self.call_sleep = call_sleep
        self.raise_on_call = raise_on_call
        self.raise_on_load = raise_on_load

    def load(self):
        if self.raise_on_load:
            raise self.raise_on_load
        time.sleep(self.load_sleep)
        with self.rec.lock:
            self.rec.loads += 1

    def transcribe(self, pcm, language, prompt):
        with self.rec.lock:
            self.rec.calls += 1
            self.rec.concurrent += 1
            self.rec.max_concurrent = max(self.rec.max_concurrent, self.rec.concurrent)
        try:
            if self.raise_on_call:
                raise self.raise_on_call
            time.sleep(self.call_sleep)
            return {"text": self.text, "language": self.language}
        finally:
            with self.rec.lock:
                self.rec.concurrent -= 1

    def unload(self):
        with self.rec.lock:
            self.rec.unloads += 1


def make_wav(seconds: float = 0.5, sr: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    """造一段 16 kHz 单声道 16 bit 的正弦 WAV —— 形状与如意麦克风发出来的那一段一致。"""
    n = int(seconds * sr)
    t = np.arange(n, dtype=np.float64) / sr
    wave_data = (0.2 * np.sin(2 * np.pi * 440.0 * t))
    if channels > 1:
        wave_data = np.repeat(wave_data[:, None], channels, axis=1).reshape(-1)
    pcm = (wave_data * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def build_multipart(fields: dict, files: list[tuple], boundary: str = "----ruyiTestBoundary1234"):
    """按如意（Node undici FormData）那边的形状拼：先若干文本字段，最后 file 段带 filename 与 content-type。

    files: [(field_name, filename, content_type, bytes)]
    回 (content_type_header, body_bytes)
    """
    b = boundary.encode("ascii")
    out = bytearray()
    for name, value in fields.items():
        out += b"--" + b + b"\r\n"
        out += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("utf-8")
        out += str(value).encode("utf-8") + b"\r\n"
    for name, filename, ctype, data in files:
        out += b"--" + b + b"\r\n"
        # 中文文件名按 Node/undici 的做法：原始 UTF-8 字节直接写进头里。
        disp = 'Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, filename)
        out += disp.encode("utf-8")
        out += ("Content-Type: %s\r\n\r\n" % ctype).encode("ascii")
        out += data + b"\r\n"
    out += b"--" + b + b"--\r\n"
    return "multipart/form-data; boundary=" + boundary, bytes(out)
