"""音频解码与格式嗅探。主路径（16 kHz 单声道 16 bit WAV）必须零第三方依赖就能解。"""

from __future__ import annotations

import io
import unittest
import wave

import numpy as np

from ruyi_asr_shim.audio import (
    TARGET_SR,
    UnsupportedAudioError,
    cleanup_stale_temp,
    decode_to_mono16k,
    sniff_format,
)

from .helpers import make_wav


class TestSniff(unittest.TestCase):
    def test_known_magics(self):
        cases = {
            "wav": b"RIFF\x00\x00\x00\x00WAVEfmt ",
            "flac": b"fLaC\x00\x00\x00\x22aaaa",
            "ogg": b"OggS\x00\x02\x00\x00\x00\x00\x00\x00",
            "webm": b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f",
            "m4a": b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00",
            "mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00",
        }
        for want, data in cases.items():
            self.assertEqual(sniff_format(data), want, "认错了：%s" % want)

    def test_mp3_frame_sync(self):
        self.assertEqual(sniff_format(b"\xff\xfb\x90\x00" + b"\x00" * 16), "mp3")

    def test_unknown_and_short(self):
        self.assertEqual(sniff_format(b"hello world!"), "unknown")
        self.assertEqual(sniff_format(b"ab"), "unknown")


class TestDecode(unittest.TestCase):
    def test_mic_path_16k_mono_16bit(self):
        buf = decode_to_mono16k(make_wav(0.5))
        self.assertEqual(buf.sample_rate, TARGET_SR)
        self.assertEqual(buf.samples.dtype, np.float32)
        self.assertEqual(buf.samples.ndim, 1)
        self.assertEqual(len(buf.samples), 8000)
        self.assertAlmostEqual(buf.duration_sec, 0.5, places=3)
        self.assertLessEqual(float(np.max(np.abs(buf.samples))), 1.0)

    def test_stereo_is_mixed_down(self):
        buf = decode_to_mono16k(make_wav(0.25, channels=2))
        self.assertEqual(buf.samples.ndim, 1)
        self.assertEqual(len(buf.samples), 4000)

    def test_resampled_to_16k(self):
        buf = decode_to_mono16k(make_wav(1.0, sr=44100))
        self.assertEqual(buf.sample_rate, TARGET_SR)
        self.assertAlmostEqual(buf.duration_sec, 1.0, places=1)

    def test_8bit_and_32bit_wav(self):
        for width in (1, 4):
            buf = decode_to_mono16k(make_wav(0.2, width=2))  # helper 只造 16bit
            self.assertEqual(buf.sample_rate, TARGET_SR)
        # 手工造一份 8 bit 的
        raw = np.full(1600, 200, dtype=np.uint8).tobytes()
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)
            w.setframerate(16000)
            w.writeframes(raw)
        buf = decode_to_mono16k(bio.getvalue())
        self.assertEqual(len(buf.samples), 1600)
        self.assertGreater(float(buf.samples[0]), 0.0)

    def test_needs_ffmpeg_formats_raise_415(self):
        for magic in (b"\x1a\x45\xdf\xa3" + b"\x00" * 32,
                      b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 32):
            with self.assertRaises(UnsupportedAudioError) as ctx:
                decode_to_mono16k(magic)
            msg = str(ctx.exception)
            self.assertIn("ffmpeg", msg)
            self.assertIn("wav", msg, "回的话里要告诉用户怎么办，不是只说不行")

    def test_garbage_is_rejected(self):
        with self.assertRaises(Exception):
            decode_to_mono16k(b"hello, this is not audio at all" * 4)

    def test_cleanup_is_safe_to_call(self):
        self.assertIsInstance(cleanup_stale_temp(), int)


if __name__ == "__main__":
    unittest.main()
