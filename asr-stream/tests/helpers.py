"""测试公用：假后端、造 PCM、起一个真 HTTP 服务。纪律：测试里【绝不】载真模型、不联网、不碰 onnxruntime。

假后端的判据刻意简单、可预测：
  · 「说话」= 样本不全为零；每 0.5 s 说话出一个「字」；
  · 端点 = 说过话之后，最后 0.8 s 全是零（与真后端 rule2 同义），或这一句累计 ≥ 20 s（rule3）；
  · reset 之后从零开始；finish 把剩下的说话当最后一句。
"""

from __future__ import annotations

import http.client
import json
import threading

import numpy as np

SR = 16000


class FakeStream:
    def __init__(self, hotwords: str):
        self.hotwords = hotwords
        self.speech = 0          # 这一句里的说话样本数
        self.trailing_silence = 0
        self.total = 0
        self.finished = False


class FakeBackend:
    name = "fake"

    def __init__(self, *, char: str = "字", endpoint_silence_sec: float = 0.8, max_utt_sec: float = 20.0, fail_on_decode=None):
        self.char = char
        self.endpoint_silence = int(endpoint_silence_sec * SR)
        self.max_utt = int(max_utt_sec * SR)
        self.fail_on_decode = fail_on_decode
        self.streams: list[FakeStream] = []

    def create_stream(self, hotwords: str):
        s = FakeStream(hotwords)
        self.streams.append(s)
        return s

    def accept(self, stream: FakeStream, samples: np.ndarray) -> None:
        stream.total += int(samples.size)
        # 逐 10 ms 帧判「说话／静音」
        frame = SR // 100
        for i in range(0, samples.size, frame):
            block = samples[i:i + frame]
            if np.any(block != 0):
                stream.speech += int(block.size)
                stream.trailing_silence = 0
            else:
                stream.trailing_silence += int(block.size)

    def decode(self, stream: FakeStream):
        if self.fail_on_decode:
            raise self.fail_on_decode
        text = self.char * (stream.speech // (SR // 2))
        endpoint = bool(stream.speech) and (stream.trailing_silence >= self.endpoint_silence or stream.speech + stream.trailing_silence >= self.max_utt)
        return text, endpoint

    def reset(self, stream: FakeStream) -> None:
        stream.speech = 0
        stream.trailing_silence = 0

    def finish(self, stream: FakeStream) -> str:
        stream.finished = True
        text = self.char * (stream.speech // (SR // 2))
        self.reset(stream)
        return text


def speech(seconds: float, amp: int = 8000) -> bytes:
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float32)
    return (np.sin(t * 0.05) * amp).astype("<i2").tobytes()


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * SR) * 2)


class Client:
    """裸 http.client，好控制 Host / Origin 头。"""

    def __init__(self, port: int):
        self.port = port

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        h = {"Host": "127.0.0.1:%d" % self.port}
        if headers:
            h.update(headers)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            j = json.loads(data.decode("utf-8")) if data else None
        except ValueError:
            j = None
        return r.status, j, data

    def open(self, hotwords=None):
        body = json.dumps({"hotwords": hotwords}).encode() if hotwords is not None else b""
        return self.request("POST", "/v1/stream/sessions", body, {"Content-Type": "application/json"})

    def audio(self, sid: str, pcm: bytes, ctype: str = "audio/L16; rate=16000"):
        return self.request("POST", "/v1/stream/sessions/%s/audio" % sid, pcm, {"Content-Type": ctype})

    def finish(self, sid: str):
        return self.request("POST", "/v1/stream/sessions/%s/finish" % sid, b"")

    def delete(self, sid: str):
        return self.request("DELETE", "/v1/stream/sessions/%s" % sid)


def start_server(manager, port: int):
    from ruyi_asr_stream.server import Settings, build_server

    settings = Settings(["--port", str(port), "--model-dir", "x"])
    httpd = build_server(settings, manager)
    th = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    return httpd, th
