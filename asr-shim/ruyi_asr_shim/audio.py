"""字节 → 模型要的输入。全程在内存里，音频【不落盘】（方案 §2.2 第 3 条）。

机器上没有 ffmpeg，所以能解什么由 libsndfile（soundfile 轮子自带）决定：
  WAV / FLAC / OGG(Vorbis, Opus) 必保；MP3 看 libsndfile 版本（1.1+ 自带 MPEG 解码）；
  M4A / MP4 / WebM 解不了 → 415，回一句人话叫用户转 wav 或装 ffmpeg。
麦克风那一路（16 kHz 单声道 16 bit WAV）是必保的主路径，它连 soundfile 都不需要：
标准库 `wave` 就能解，少一个依赖少一处坏。
"""

from __future__ import annotations

import glob
import io
import os
import tempfile
import wave
from dataclasses import dataclass

import numpy as np

TARGET_SR = 16000
TEMP_PREFIX = "ruyi-asr-shim-"


class UnsupportedAudioError(Exception):
    """格式识别出来了，但本机解不了（回 415）。"""


@dataclass
class AudioBuffer:
    samples: np.ndarray  # float32, 一维, [-1, 1]
    sample_rate: int

    @property
    def duration_sec(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return float(len(self.samples)) / float(self.sample_rate)


# ── 格式嗅探 ────────────────────────────────────────────────────────────────

def sniff_format(data: bytes) -> str:
    """按魔数认格式。认不出回 'unknown'。不信任调用方给的 content-type。"""
    if len(data) < 12:
        return "unknown"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"  # EBML：webm / mkv
    if data[4:8] == b"ftyp":
        return "m4a"  # ISO-BMFF 家族：m4a / mp4 / mov，本机都解不了
    if data[:3] == b"ID3":
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[:4] == b"caff":
        return "caf"
    if data[:5] == b"#!AMR":
        return "amr"
    return "unknown"


_NEED_FFMPEG = {
    "m4a": "m4a/mp4",
    "webm": "webm",
    "caf": "caf",
    "amr": "amr",
}


# ── 解码 ────────────────────────────────────────────────────────────────────

def decode_to_mono16k(data: bytes, declared_type: str = "") -> AudioBuffer:
    """任意支持的音频字节 → 16 kHz 单声道 float32。解不了抛 UnsupportedAudioError。"""
    fmt = sniff_format(data)
    if fmt in _NEED_FFMPEG:
        raise UnsupportedAudioError(
            "这个 shim 解不了 %s 格式（本机没有 ffmpeg）。"
            "请先转成 wav / flac / ogg，或者给机器装上 ffmpeg 后改用别的转写服务。"
            % _NEED_FFMPEG[fmt]
        )

    samples, sr = None, 0
    if fmt == "wav":
        try:
            samples, sr = _decode_wav_stdlib(data)
        except Exception:
            samples, sr = None, 0  # 非 PCM 的 WAV（float / ADPCM）交给 soundfile
    if samples is None:
        samples, sr = _decode_soundfile(data, fmt)

    if samples.ndim > 1:  # 多声道 → 混单声道
        samples = samples.mean(axis=1)
    samples = np.ascontiguousarray(samples, dtype=np.float32)
    if sr != TARGET_SR:
        samples = _resample(samples, sr, TARGET_SR)
        sr = TARGET_SR
    if samples.size == 0:
        raise ValueError("音频里一个采样点都没有")
    return AudioBuffer(samples=samples, sample_rate=sr)


def _decode_wav_stdlib(data: bytes) -> tuple[np.ndarray, int]:
    """标准库解 PCM WAV。主路径（麦克风）走这里，零第三方依赖。"""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getcomptype() != "NONE":
            raise ValueError("压缩 WAV，交给 soundfile")
        n_ch = w.getnchannels()
        width = w.getsampwidth()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 1:  # 8 bit WAV 是无符号的
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        val = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)).astype(np.int32)
        val = np.where(val & 0x800000, val - 0x1000000, val)
        arr = val.astype(np.float32) / 8388608.0
    else:
        raise ValueError("未知的 WAV 采样位宽：%d" % width)
    if n_ch > 1:
        arr = arr[: (len(arr) // n_ch) * n_ch].reshape(-1, n_ch)
    return arr, sr


def _decode_soundfile(data: bytes, fmt: str) -> tuple[np.ndarray, int]:
    try:
        import soundfile as sf
    except ImportError as exc:
        raise UnsupportedAudioError(
            "本机没装 soundfile，只能解 PCM 的 wav。请先 pip install soundfile，或把音频转成 16 kHz 单声道 wav。"
        ) from exc
    try:
        samples, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
    except Exception as exc:
        hint = "" if fmt != "mp3" else "（这台机器的 libsndfile 可能不带 MPEG 解码；把 mp3 转成 wav 再试）"
        raise UnsupportedAudioError(
            "解不了这段音频%s：%s" % (hint, str(exc)[:200])
        ) from exc
    return samples, int(sr)


def _resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """重采样。有 soxr / scipy 就用它们（质量好），都没有就退线性插值。

    麦克风那一路本来就是 16 kHz，走不到这里；这条路只服务用户丢进来的杂牌采样率文件。
    """
    if src_sr == dst_sr or x.size == 0:
        return x
    try:
        import soxr  # type: ignore

        return np.ascontiguousarray(soxr.resample(x, src_sr, dst_sr), dtype=np.float32)
    except ImportError:
        pass
    try:
        from math import gcd

        from scipy.signal import resample_poly  # type: ignore

        g = gcd(int(src_sr), int(dst_sr))
        return np.ascontiguousarray(
            resample_poly(x, dst_sr // g, src_sr // g), dtype=np.float32
        )
    except ImportError:
        pass
    n_out = int(round(len(x) * dst_sr / float(src_sr)))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    idx = np.linspace(0.0, len(x) - 1.0, n_out, dtype=np.float64)
    return np.interp(idx, np.arange(len(x), dtype=np.float64), x).astype(np.float32)


# ── 临时文件残留清理 ────────────────────────────────────────────────────────

def cleanup_stale_temp() -> int:
    """启动／退出时清掉上次跑崩留下的临时文件。

    正常路径【根本不落盘】（全部在内存里解码），这个函数存在只是为了兜住「以后有人加了落盘
    分支又崩了」的情况，以及清掉旧版本的残留。
    """
    removed = 0
    pattern = os.path.join(tempfile.gettempdir(), TEMP_PREFIX + "*")
    for path in glob.glob(pattern):
        try:
            if os.path.isfile(path):
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed
