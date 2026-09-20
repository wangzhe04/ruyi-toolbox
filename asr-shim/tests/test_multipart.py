"""multipart 解析的单测（方案 §2.7 第一组判据）。

Python 3.13 删了 cgi 模块，这一层是我们自己写的，所以它的边界情况必须逐条钉住。
"""

from __future__ import annotations

import unittest

from ruyi_asr_shim.multipart import (
    MultipartError,
    boundary_from_content_type,
    field_value,
    find_part,
    parse_multipart,
)

from .helpers import build_multipart, make_wav


class TestBoundary(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(
            boundary_from_content_type("multipart/form-data; boundary=abc123"), b"abc123"
        )

    def test_quoted_and_case(self):
        self.assertEqual(
            boundary_from_content_type('MULTIPART/Form-Data; BOUNDARY="ab c"'), b"ab c"
        )

    def test_not_multipart(self):
        with self.assertRaises(MultipartError):
            boundary_from_content_type("application/json")

    def test_missing_boundary(self):
        with self.assertRaises(MultipartError):
            boundary_from_content_type("multipart/form-data")

    def test_too_long(self):
        with self.assertRaises(MultipartError):
            boundary_from_content_type("multipart/form-data; boundary=" + "x" * 71)


class TestParse(unittest.TestCase):
    def test_normal_request_shape(self):
        """如意 transcribeAudioViaProvider 发出来的那一份：model/response_format/language/file。"""
        audio = make_wav(0.2)
        ctype, body = build_multipart(
            {"model": "qwen3-asr-0.6b", "response_format": "json", "language": "zh"},
            [("file", "clip.wav", "audio/wav", audio)],
        )
        parts = parse_multipart(body, boundary_from_content_type(ctype))
        self.assertEqual(len(parts), 4)
        self.assertEqual(field_value(parts, "model"), "qwen3-asr-0.6b")
        self.assertEqual(field_value(parts, "response_format"), "json")
        self.assertEqual(field_value(parts, "language"), "zh")
        f = find_part(parts, "file")
        self.assertTrue(f.is_file)
        self.assertEqual(f.filename, "clip.wav")
        self.assertEqual(f.content_type, "audio/wav")
        self.assertEqual(f.data, audio)

    def test_chinese_filename(self):
        ctype, body = build_multipart({}, [("file", "会议录音 2026.wav", "audio/wav", b"RIFFxxxx")])
        parts = parse_multipart(body, boundary_from_content_type(ctype))
        self.assertEqual(find_part(parts, "file").filename, "会议录音 2026.wav")

    def test_rfc5987_filename(self):
        boundary = b"BB"
        body = (
            b"--BB\r\n"
            b"Content-Disposition: form-data; name=\"file\"; filename*=UTF-8''%E4%B8%AD%E6%96%87.wav\r\n"
            b"Content-Type: audio/wav\r\n\r\n"
            b"data\r\n"
            b"--BB--\r\n"
        )
        parts = parse_multipart(body, boundary)
        self.assertEqual(parts[0].filename, "中文.wav")

    def test_missing_file_field(self):
        ctype, body = build_multipart({"model": "x"}, [])
        parts = parse_multipart(body, boundary_from_content_type(ctype))
        self.assertIsNone(find_part(parts, "file"))

    def test_boundary_string_inside_content(self):
        """内容里裸出现 boundary 串（甚至带 -- 前缀）不能把体切坏。"""
        boundary = "----ruyiTestBoundary1234"
        payload = (
            b"before--" + boundary.encode() + b"middle\r\n"
            b"--" + boundary.encode() + b" not-a-delimiter\r\n"
            b"tail"
        )
        ctype, body = build_multipart({}, [("file", "a.bin", "application/octet-stream", payload)])
        parts = parse_multipart(body, boundary.encode())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].data, payload)

    def test_crlf_inside_content(self):
        payload = b"line1\r\nline2\r\n\r\nline3"
        ctype, body = build_multipart({}, [("file", "a.bin", "application/octet-stream", payload)])
        parts = parse_multipart(body, boundary_from_content_type(ctype))
        self.assertEqual(parts[0].data, payload)

    def test_binary_payload_roundtrip(self):
        payload = bytes(range(256)) * 8
        ctype, body = build_multipart({}, [("file", "a.bin", "application/octet-stream", payload)])
        parts = parse_multipart(body, boundary_from_content_type(ctype))
        self.assertEqual(parts[0].data, payload)

    def test_truncated_body(self):
        ctype, body = build_multipart({}, [("file", "a.wav", "audio/wav", b"12345")])
        with self.assertRaises(MultipartError):
            parse_multipart(body[: len(body) - 12], boundary_from_content_type(ctype))

    def test_no_delimiter_at_all(self):
        with self.assertRaises(MultipartError):
            parse_multipart(b"just some bytes", b"BB")

    def test_part_without_disposition(self):
        body = b"--BB\r\nContent-Type: text/plain\r\n\r\nhi\r\n--BB--\r\n"
        with self.assertRaises(MultipartError):
            parse_multipart(body, b"BB")

    def test_too_many_parts(self):
        fields = {("f%d" % i): "v" for i in range(40)}
        ctype, body = build_multipart(fields, [])
        with self.assertRaises(MultipartError):
            parse_multipart(body, boundary_from_content_type(ctype), max_parts=8)

    def test_empty_boundary_rejected(self):
        with self.assertRaises(MultipartError):
            parse_multipart(b"whatever", b"")


if __name__ == "__main__":
    unittest.main()
