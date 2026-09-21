"""multipart/form-data 解析。

为什么自己写：Python 3.13 已经删掉了标准库 `cgi` 模块（PEP 594），`cgi.FieldStorage` 没得用了；
`email` 包能解，但它按文本行工作、对二进制体要绕一圈 `BytesParser` 并且会在 header 折行上做
它自己那套 RFC 2047 解码——音频件走它反而更容易出奇怪的事。这里只解我们真正要的那一小块：
RFC 7578 形的 multipart/form-data，若干文本字段 + 一个文件字段。

设计要点（都有单测钉住）：
- 分隔符必须是【CRLF + "--" + boundary】且其后紧跟 CRLF 或 "--"。只有这样，内容里出现的
  boundary 串才不会把体切坏。
- part 头按字节解析，`Content-Disposition` 的 name / filename 支持带引号、引号内转义，
  以及 RFC 5987 的 `filename*=UTF-8''%e4%bd%a0`。
- 文件名只做记录用（我们不落盘、日志里也不记），这里不做路径清洗——调用方不得拿它拼路径。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import unquote_to_bytes

CRLF = b"\r\n"


class MultipartError(ValueError):
    """请求体不是一个能解的 multipart/form-data（调用方应回 400）。"""


@dataclass
class Part:
    name: str = ""
    filename: str | None = None
    content_type: str = ""
    data: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_file(self) -> bool:
        return self.filename is not None

    def text(self, limit: int = 4096) -> str:
        return self.data[:limit].decode("utf-8", "replace")


def boundary_from_content_type(content_type: str) -> bytes:
    """从 Content-Type 头里抠出 boundary；不是 multipart/form-data 就抛 MultipartError。"""
    ct = str(content_type or "")
    main = ct.split(";", 1)[0].strip().lower()
    if main != "multipart/form-data":
        raise MultipartError(
            "Content-Type 必须是 multipart/form-data，收到的是 " + (main or "(空)")
        )
    params = _parse_params(ct)
    raw = params.get("boundary", "")
    if not raw:
        raise MultipartError("multipart/form-data 缺少 boundary 参数")
    # RFC 2046：boundary 最长 70 个字符，只许 US-ASCII。
    if len(raw) > 70:
        raise MultipartError("boundary 过长")
    try:
        return raw.encode("ascii")
    except UnicodeEncodeError as exc:  # pragma: no cover - 实际极罕见
        raise MultipartError("boundary 含非 ASCII 字符") from exc


def parse_multipart(body: bytes, boundary: bytes, max_parts: int = 32) -> list[Part]:
    """把整块请求体解成 Part 列表。体已经在 server 层被 25 MB 闸挡过，这里可以整块吃。"""
    if not boundary:
        raise MultipartError("boundary 为空")
    sep = b"--" + boundary

    # 找开头那道分隔符（前面允许有 preamble，按 RFC 忽略）。
    start = _find_delimiter(body, sep, 0, allow_at_zero=True)
    if start < 0:
        raise MultipartError("请求体里找不到起始分隔符")

    parts: list[Part] = []
    pos = start
    while True:
        # pos 始终指向【分隔符本身】的起点（_find_delimiter 的返回口径）。
        after = pos + len(sep)
        tail = body[after : after + 2]
        if tail == b"--":
            break  # 收尾分隔符，正常结束
        if tail != CRLF:
            raise MultipartError("分隔符后既不是 CRLF 也不是收尾的 --")
        head_start = after + 2

        nxt = _find_delimiter(body, sep, head_start, allow_at_zero=False)
        if nxt < 0:
            raise MultipartError("请求体在中途结束：找不到收尾分隔符")
        # nxt 指向分隔符起点，它前面那对 CRLF 属于分隔符、不属于内容。
        part_end = nxt - len(CRLF)
        parts.append(_parse_part(body[head_start:part_end]))
        if len(parts) > max_parts:
            raise MultipartError("multipart 段数过多")
        pos = nxt

    if not parts:
        raise MultipartError("multipart 里一个段都没有")
    return parts


def find_part(parts: Iterable[Part], name: str) -> Part | None:
    for p in parts:
        if p.name == name:
            return p
    return None


def field_value(parts: Iterable[Part], name: str, limit: int = 4096) -> str:
    """取一个文本字段的值；不存在回空串。文件段不算文本字段。"""
    for p in parts:
        if p.name == name and not p.is_file:
            return p.text(limit)
    return ""


# ── 内部实现 ────────────────────────────────────────────────────────────────


def _find_delimiter(body: bytes, sep: bytes, from_pos: int, *, allow_at_zero: bool) -> int:
    """找下一道【真】分隔符，返回 "--boundary" 的起点；找不到回 -1。

    真分隔符的判据：要么落在体的最开头（allow_at_zero），要么前面紧跟 CRLF；
    并且其后紧跟 CRLF 或 "--"。内容里裸出现的 boundary 串过不了这两关。
    """
    if allow_at_zero and from_pos == 0 and body.startswith(sep):
        tail = body[len(sep) : len(sep) + 2]
        if tail == CRLF or tail == b"--":
            return 0

    needle = CRLF + sep
    i = from_pos
    while True:
        i = body.find(needle, i)
        if i < 0:
            return -1
        cand = i + len(CRLF)
        tail = body[cand + len(sep) : cand + len(sep) + 2]
        if tail == CRLF or tail == b"--":
            return cand
        i += 1


def _parse_part(chunk: bytes) -> Part:
    split = chunk.find(CRLF + CRLF)
    if split < 0:
        raise MultipartError("multipart 段里找不到头与体的分界（空行）")
    head_bytes = chunk[:split]
    data = chunk[split + 4 :]

    headers: dict[str, str] = {}
    for line in head_bytes.split(CRLF):
        if not line:
            continue
        # 头按 UTF-8 解；Node/undici 的 FormData 会把中文文件名以【原始 UTF-8 字节】写进
        # Content-Disposition，latin-1 解会得到乱码，所以这里必须 utf-8。
        text = line.decode("utf-8", "replace")
        if ":" not in text:
            raise MultipartError("multipart 段头格式不对")
        k, v = text.split(":", 1)
        headers[k.strip().lower()] = v.strip()

    disp = headers.get("content-disposition", "")
    if not disp or disp.split(";", 1)[0].strip().lower() != "form-data":
        raise MultipartError("multipart 段缺少 Content-Disposition: form-data")
    params = _parse_params(disp)

    filename = params.get("filename*")
    if filename is None:
        filename = params.get("filename")
    name = params.get("name", "")

    return Part(
        name=name,
        filename=filename,
        content_type=headers.get("content-type", ""),
        data=data,
        headers=headers,
    )


_TOKEN_PARAM = re.compile(
    r';\s*([A-Za-z0-9!#$%&\'*+.^_`|~-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]*))'
)


def _parse_params(header_value: str) -> dict[str, str]:
    """解 `a=b; c="d"; e*=UTF-8''%..` 这种参数串。带 * 的按 RFC 5987 解，键名保留 `*`。"""
    out: dict[str, str] = {}
    if ";" not in header_value:
        return out
    # 丢掉 ';' 前的主值（form-data / multipart-form-data），只留参数串，并补回引导的 ';'。
    tail = ";" + header_value.split(";", 1)[1]
    for m in _TOKEN_PARAM.finditer(tail):
        key = m.group(1).lower()
        if m.group(2) is not None:
            val = re.sub(r"\\(.)", r"\1", m.group(2))
        else:
            val = (m.group(3) or "").strip()
        if key.endswith("*"):
            out[key] = _decode_ext_param(val)
        else:
            out[key] = val
    return out


def _decode_ext_param(raw: str) -> str:
    """RFC 5987 `charset'lang'pct-encoded`。解不动就原样回，别为一个文件名把请求打翻。"""
    bits = raw.split("'", 2)
    if len(bits) != 3:
        return raw
    charset = bits[0] or "utf-8"
    try:
        return unquote_to_bytes(bits[2]).decode(charset, "replace")
    except (LookupError, UnicodeDecodeError):
        return raw
