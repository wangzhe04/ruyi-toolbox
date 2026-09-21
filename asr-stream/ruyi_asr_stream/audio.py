"""离线整句识别用的音频解码：**只认 WAV（PCM）**，任意采样率／声道 → 16 kHz 单声道 float32。

为什么只认 WAV：本组件零第三方依赖（不装 soundfile／ffmpeg）；如意麦克风那一路送来的本来就是 16 kHz 单声道 WAV，
附件里的 mp3／webm 请交给 asr-shim 或云端识别（如意会按 415 报「这个端点解不了这种格式」）。
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

import numpy as np

TARGET_SR = 16000


class UnsupportedAudioError(ValueError):
    pass


@dataclass
class AudioBuffer:
    samples: np.ndarray  # float32、一维、[-1, 1]、16 kHz
    sample_rate: int

    @property
    def duration_sec(self) -> float:
        return float(len(self.samples)) / float(self.sample_rate) if self.sample_rate > 0 else 0.0


def decode_wav_to_mono16k(data: bytes) -> AudioBuffer:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise UnsupportedAudioError("本端点只认 WAV（PCM）。mp3／webm／m4a 这类请交给 asr-shim（Qwen3-ASR）或云端识别。")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getcomptype() != "NONE":
                raise UnsupportedAudioError("压缩编码的 WAV 解不了，只认 PCM WAV。")
            n_ch, width, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except wave.Error as exc:
        raise UnsupportedAudioError("WAV 头解析失败：%s" % (str(exc)[:120])) from exc
    if width == 1:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8)
        b = b[: (len(b) // 3) * 3].reshape(-1, 3).astype(np.int32)
        val = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)).astype(np.int32)
        val = np.where(val & 0x800000, val - 0x1000000, val)
        arr = val.astype(np.float32) / 8388608.0
    else:
        raise UnsupportedAudioError("未知的 WAV 采样位宽：%d" % width)
    if n_ch > 1:
        arr = arr[: (len(arr) // n_ch) * n_ch].reshape(-1, n_ch).mean(axis=1)
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    if sr != TARGET_SR:
        arr = resample_linear(arr, sr, TARGET_SR)
    if arr.size == 0:
        raise UnsupportedAudioError("音频里一个采样点都没有。")
    return AudioBuffer(samples=arr, sample_rate=TARGET_SR)


def resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """线性插值重采样：只服务杂牌采样率的 WAV，麦克风那一路本来就是 16 kHz。"""
    if src_sr == dst_sr or x.size == 0:
        return x
    n_out = int(round(len(x) * dst_sr / float(src_sr)))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    idx = np.linspace(0.0, len(x) - 1.0, n_out, dtype=np.float64)
    return np.interp(idx, np.arange(len(x), dtype=np.float64), x).astype(np.float32)
