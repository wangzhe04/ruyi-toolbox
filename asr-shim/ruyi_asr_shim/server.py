"""HTTP 层：标准库 http.server，只绑 127.0.0.1，OpenAI 兼容的转写端点。

为什么是标准库而不是 FastAPI：这个 shim 的价值在于「用户装起来不容易坏」。多一个 web 框架
就多一串依赖、多一次版本冲突。路由一共三条，标准库够用。

路由：
  GET  /health                    进程活着即 200；【不触发模型加载】
  GET  /v1/models                 OpenAI 形的模型列表（如意的「测试连接」会打它）；auto 模式下列出 auto ＋ 每份装好的尺寸
  POST /v1/audio/transcriptions   Whisper 形转写（如意 transcribeAudioViaProvider 的缺省协议）；model 字段选尺寸，换了就换加载
  POST /v1/unload                 立刻卸载已加载的模型、释放显存（第 133 波：如意在用户把语音识别切走时打它）

安全闸（方案 §2.2，不许放宽）：只绑 127.0.0.1；Host 头必须是 127.0.0.1:<port> / localhost:<port>，
否则 403（防 DNS rebinding）；带 Origin 头一律 403（如意服务端用 Node fetch 出站，不带 Origin）；
不回任何 CORS 头。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import COMPONENT_NAME_TAG, __version__
from . import audio as audio_mod
from .audio import UnsupportedAudioError
from .engine import EngineError, EngineManager
from .watchdog import watchdog_from_env

LOG = logging.getLogger("ruyi_asr_shim")

BIND_HOST = "127.0.0.1"  # 硬编码，不提供改绑开关（方案 §2.2 第 1 条）
DEFAULT_PORT = 8790
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 与主仓 ASR_MAX_BODY_BYTES 同值
MAX_HEADER_FIELD = 4096

# 如意侧要填的模型名 ←→ 实际的 HuggingFace/ModelScope 仓库名。
# 注意是 `-hf` 那份：原生 transformers 只认它（依据见 docs/backend-notes.md §1）。
MODEL_ALIASES = {
    "qwen3-asr-0.6b": "Qwen/Qwen3-ASR-0.6B-hf",
    "qwen3-asr-1.7b": "Qwen/Qwen3-ASR-1.7B-hf",
}


class Settings:
    """全部可配项只走环境变量／命令行；绝不读写如意的数据目录。"""

    def __init__(self, argv: list[str] | None = None):
        env = os.environ
        ap = argparse.ArgumentParser(prog="python -m ruyi_asr_shim", add_help=True)
        ap.add_argument("--port", type=int, default=_env_int(env, "RUYI_ASR_PORT", DEFAULT_PORT))
        ap.add_argument("--model", default=env.get("RUYI_ASR_MODEL", "qwen3-asr-0.6b").strip())
        ap.add_argument("--model-dir", default=env.get("RUYI_ASR_MODEL_DIR", "").strip())
        # 「auto」：models 目录里有几份就按显存挑最大能装下的（autopick.py）；此时 --model-dir 不用给
        ap.add_argument("--models-root", default=env.get("RUYI_ASR_MODELS_ROOT", "").strip())
        ap.add_argument(
            "--idle-unload-sec",
            type=int,
            default=_env_int(env, "RUYI_ASR_IDLE_UNLOAD_SEC", 600),
        )
        # auto/cuda/amd/rocm/xpu/mps/directml/cpu —— 含义与降级顺序见 devices.py
        ap.add_argument("--device", default=env.get("RUYI_ASR_DEVICE", "auto").strip() or "auto")
        ap.add_argument("--dtype", default=env.get("RUYI_ASR_DTYPE", "auto").strip() or "auto")
        ap.add_argument("--log-level", default=env.get("RUYI_ASR_LOG_LEVEL", "INFO").strip() or "INFO")
        # auto 挑哪份：small（缺省，省显存）／ large（按空闲显存挑最大能装下的，老策略）
        ap.add_argument("--auto-prefer", default=env.get("RUYI_ASR_AUTO_PREFER", "small").strip() or "small")
        ns = ap.parse_args([] if argv is None else argv)

        self.port: int = int(ns.port)
        from .autopick import AUTO_MODEL_NAME, is_auto, normalize_prefer  # noqa: PLC0415

        self.auto: bool = is_auto(ns.model)
        self.model_name: str = AUTO_MODEL_NAME if self.auto else (ns.model or "qwen3-asr-0.6b")
        self.model_repo: str = "" if self.auto else MODEL_ALIASES.get(self.model_name.lower(), self.model_name)
        self.model_dir: str = "" if self.auto else ns.model_dir
        self.models_root: str = ns.models_root
        self.prefer: str = normalize_prefer(ns.auto_prefer)
        self.idle_unload_sec: int = max(0, int(ns.idle_unload_sec))
        self.device: str = ns.device
        self.dtype: str = ns.dtype
        self.log_level: str = ns.log_level

    def catalog(self) -> list[dict]:
        """如意能选的模型清单。auto 模式：auto ＋ models 目录里每份装好的尺寸（现读现算，下了新模型不用重启）；
        单模型模式：就那一份。"""
        if self.auto:
            from .autopick import catalog  # noqa: PLC0415

            return catalog(self.models_root)
        return [{"id": self.model_name, "label": self.model_name}]

    def resolve_model(self, requested: str) -> str:
        """请求里的 model 字段 → 给引擎的名字。空／不认识的名字 → 缺省（如意填 whisper-1 也照转，老判据）；
        auto 模式下认 auto 与清单里的每份尺寸。"""
        key = str(requested or "").strip().lower()
        if not key:
            return self.model_name
        if not self.auto:
            return self.model_name
        from .autopick import AUTO_MODEL_NAME, is_auto  # noqa: PLC0415

        if is_auto(key):
            return AUTO_MODEL_NAME
        for m in self.catalog():
            if m["id"] == key:
                return key
        return self.model_name


def _env_int(env, key: str, default: int) -> int:
    raw = str(env.get(key, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class ShimHandler(BaseHTTPRequestHandler):
    server_version = "ruyi-asr-shim"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # 由 serve() 注入
    settings: Settings
    manager: EngineManager

    # ── 基础设施 ───────────────────────────────────────────────────────────

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 覆写基类
        # 基类默认往 stderr 打「请求行」，里面含 query string。我们自己记元数据，这里闭嘴。
        LOG.debug("http %s", fmt % args)

    def _send(self, status: int, payload: dict, *, close: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 不回任何 CORS 头；本机自用，也不给浏览器任何「可以跨站用我」的暗示。
        self.send_header("X-Content-Type-Options", "nosniff")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _fail(self, status: int, err_type: str, message: str, *, close: bool = False) -> None:
        """错误体必须是一句人话 JSON —— 主仓会把回体前 1000 字直接展示给用户看。"""
        self._send(status, {"error": {"message": message, "type": err_type}}, close=close)

    def _gate(self) -> bool:
        """安全闸：过了回 True，没过自己已经回完 403。"""
        origin = self.headers.get("Origin")
        if origin is not None:
            LOG.warning("gate reject: origin header present")
            self._fail(
                403,
                "forbidden",
                "本服务只服务本机程序，不接受浏览器跨站请求（带 Origin 头一律拒绝）。",
                close=True,
            )
            return False
        host = str(self.headers.get("Host") or "").strip().lower()
        allowed = {
            f"127.0.0.1:{self.settings.port}",
            f"localhost:{self.settings.port}",
        }
        if host not in allowed:
            LOG.warning("gate reject: bad host header")
            self._fail(
                403,
                "forbidden",
                "Host 头必须是 127.0.0.1:%d 或 localhost:%d（防 DNS rebinding）。"
                % (self.settings.port, self.settings.port),
                close=True,
            )
            return False
        return True

    def _path(self) -> str:
        raw = self.path or "/"
        return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/"

    # ── 路由 ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 - 基类约定
        if not self._gate():
            return
        path = self._path()
        if path == "/health":
            return self._health()
        if path == "/v1/models":
            return self._models()
        if path in ("/v1/audio/transcriptions", "/v1/unload"):
            return self._fail(405, "method_not_allowed", "该端点只接受 POST。")
        return self._fail(404, "not_found", "没有这条路径。本服务只有 /health、/v1/models、/v1/audio/transcriptions、/v1/unload。")

    def do_POST(self) -> None:  # noqa: N802
        if not self._gate():
            return
        path = self._path()
        if path == "/v1/audio/transcriptions":
            return self._transcribe()
        if path == "/v1/unload":
            return self._unload()
        if path in ("/health", "/v1/models"):
            return self._fail(405, "method_not_allowed", "该端点只接受 GET。", close=True)
        return self._fail(
            404,
            "not_found",
            "没有这条路径。本服务只有 /health、/v1/models、/v1/audio/transcriptions、/v1/unload。",
            close=True,
        )

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._gate():
            return
        self._fail(405, "method_not_allowed", "本服务不支持 HEAD。")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._gate():
            return
        self._fail(405, "method_not_allowed", "本服务只接受 GET 与 POST。", close=True)

    do_DELETE = do_PUT
    do_PATCH = do_PUT
    do_OPTIONS = do_HEAD  # 不做 CORS 预检，一律 405

    # ── 处理器 ─────────────────────────────────────────────────────────────

    def _health(self) -> None:
        # 判据：/health【不得】触发模型加载，也不得 import torch。status() 只读状态。
        # component / version 是组件登记约定 §2.2 要求的：如意靠 component 认出
        # 「这个端口上活着的就是我登记的那个组件」，从而不去拉第二个。
        st = self.manager.status()
        self._send(
            200,
            {
                "ok": True,
                "component": COMPONENT_NAME_TAG,
                "version": __version__,
                "model": self.settings.model_name,
                # 加载后才知道实际是哪份（auto 挑的／请求点名的）；没加载就是缺省名
                "resolvedModel": st.get("model") or self.settings.model_name,
                "loaded": st["loaded"],
                "device": st["device"],
                "idleUnloadSec": self.settings.idle_unload_sec,
                "models": [m["id"] for m in self.settings.catalog()],
            },
        )

    def _models(self) -> None:
        self._send(
            200,
            {
                "object": "list",
                "data": [
                    {"id": m["id"], "object": "model", "created": 0, "owned_by": "ruyi-asr-shim", "label": m["label"]}
                    for m in self.settings.catalog()
                ],
            },
        )

    def _unload(self) -> None:
        """立刻卸载：在途的那一发转完才卸（引擎那把锁），所以可能等上一两秒。不读体、不要求体。"""
        # 体若有就吃掉（Content-Length 为 0 或没有都行），别让残余字节污染下一个请求。
        raw_len = self.headers.get("Content-Length")
        try:
            n = int(raw_len) if raw_len else 0
        except ValueError:
            n = 0
        if 0 < n <= 65536:
            self.rfile.read(n)
        elif n > 65536:
            self._fail(413, "payload_too_large", "/v1/unload 不收请求体。", close=True)
            return
        t0 = time.monotonic()
        did = self.manager.unload_now()
        LOG.info("unload requested did=%s ms=%d", did, int((time.monotonic() - t0) * 1000))
        self._send(200, {"ok": True, "unloaded": bool(did), "loaded": False})

    def _read_body(self) -> bytes | None:
        """读请求体，带 25 MB 双道闸（Content-Length 预检 + 累计），超了自己回 413。"""
        te = str(self.headers.get("Transfer-Encoding") or "").strip().lower()
        if "chunked" in te:
            return self._read_chunked()
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._fail(411, "length_required", "请求缺少 Content-Length。", close=True)
            return None
        try:
            length = int(raw_len)
        except ValueError:
            self._fail(400, "bad_request", "Content-Length 不是数字。", close=True)
            return None
        if length < 0:
            self._fail(400, "bad_request", "Content-Length 为负。", close=True)
            return None
        if length > MAX_UPLOAD_BYTES:
            self._too_large()
            return None
        buf = bytearray()
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                self._fail(400, "bad_request", "请求体提前结束。", close=True)
                return None
            buf.extend(chunk)
            remaining -= len(chunk)
        return bytes(buf)

    def _read_chunked(self) -> bytes | None:
        buf = bytearray()
        while True:
            line = self.rfile.readline(1024)
            if not line:
                self._fail(400, "bad_request", "chunked 请求体提前结束。", close=True)
                return None
            size_txt = line.split(b";", 1)[0].strip()
            try:
                size = int(size_txt, 16)
            except ValueError:
                self._fail(400, "bad_request", "chunked 分块长度不合法。", close=True)
                return None
            if size == 0:
                # 吃掉 trailer 到空行
                while True:
                    t = self.rfile.readline(1024)
                    if not t or t in (b"\r\n", b"\n"):
                        break
                return bytes(buf)
            if len(buf) + size > MAX_UPLOAD_BYTES:
                self._too_large()
                return None
            need = size
            while need > 0:
                chunk = self.rfile.read(min(65536, need))
                if not chunk:
                    self._fail(400, "bad_request", "chunked 请求体提前结束。", close=True)
                    return None
                buf.extend(chunk)
                need -= len(chunk)
            self.rfile.read(2)  # 分块末尾的 CRLF

    def _too_large(self) -> None:
        # 413 时客户端多半还在续传，不要试图 drain —— 直接回完关连接。
        self._fail(
            413,
            "payload_too_large",
            "音频超过 25 MB 上限。",
            close=True,
        )

    def _transcribe(self) -> None:
        t0 = time.monotonic()
        ctype = str(self.headers.get("Content-Type") or "")
        main = ctype.split(";", 1)[0].strip().lower()
        if main != "multipart/form-data":
            # 体还在线上没读，回完就关，别让残余字节被当成下一个请求。
            self._fail(
                400,
                "bad_request",
                "本端点只接受 multipart/form-data（OpenAI /v1/audio/transcriptions 形）。",
                close=True,
            )
            return

        body = self._read_body()
        if body is None:
            return

        from .multipart import MultipartError, boundary_from_content_type, field_value, find_part, parse_multipart

        try:
            boundary = boundary_from_content_type(ctype)
            parts = parse_multipart(body, boundary)
        except MultipartError as exc:
            LOG.info("asr reject: bad multipart (%s bytes)", len(body))
            self._fail(400, "bad_request", "multipart 解析失败：" + str(exc))
            return

        file_part = find_part(parts, "file")
        if file_part is None or not file_part.is_file:
            LOG.info("asr reject: missing file field")
            self._fail(400, "bad_request", "缺少 file 字段（OpenAI 形的音频文件段）。")
            return
        if not file_part.data:
            LOG.info("asr reject: empty audio")
            self._fail(400, "bad_request", "file 段是空的。")
            return

        requested_model = field_value(parts, "model", MAX_HEADER_FIELD).strip()
        language = field_value(parts, "language", 64).strip()[:40] or None
        prompt = field_value(parts, "prompt", MAX_HEADER_FIELD).strip() or None
        response_format = (field_value(parts, "response_format", 64).strip() or "json").lower()
        if response_format not in ("json", "verbose_json", "text", ""):
            LOG.info("asr reject: response_format=%s", response_format[:20])
            self._fail(
                400,
                "bad_request",
                "response_format 只支持 json / verbose_json / text，收到的是 " + response_format[:20],
            )
            return

        nbytes = len(file_part.data)
        try:
            pcm = audio_mod.decode_to_mono16k(file_part.data, declared_type=file_part.content_type)
        except UnsupportedAudioError as exc:
            LOG.info("asr reject: unsupported format bytes=%d", nbytes)
            self._fail(415, "unsupported_media_type", str(exc))
            return
        except Exception as exc:  # 解码失败：格式对但坏了
            LOG.info("asr reject: decode failed bytes=%d err=%s", nbytes, type(exc).__name__)
            self._fail(400, "bad_request", "音频解码失败：" + _short(exc))
            return

        try:
            result = self.manager.transcribe(pcm, language=language, prompt=prompt,
                                             model=self.settings.resolve_model(requested_model))
        except UnsupportedAudioError as exc:
            LOG.info("asr reject: engine rejected audio")
            self._fail(415, "unsupported_media_type", str(exc))
            return
        except EngineError as exc:
            LOG.error("asr engine error: %s", _short(exc))
            self._fail(500, "engine_error", "本地模型出错：" + _short(exc))
            return
        except Exception as exc:  # 兜底：绝不把 traceback 泄给调用方
            LOG.exception("asr unexpected error")
            self._fail(500, "internal_error", "本地 shim 内部错误：" + _short(exc))
            return

        dur_ms = int((time.monotonic() - t0) * 1000)
        text = str(result.get("text", ""))
        out_lang = str(result.get("language") or "") or None
        # 日志只记元数据：不记转写文本、不记文件名（方案 §2.2 第 4 条）。
        LOG.info(
            "asr ok bytes=%d audio_s=%.2f dur_ms=%d lang=%s text_len=%d model_req=%s",
            nbytes,
            pcm.duration_sec,
            dur_ms,
            out_lang or "-",
            len(text),
            _safe_token(requested_model),
        )

        if response_format == "text":
            self._send_text(text)
            return

        payload: dict = {"text": text}
        if out_lang:
            payload["language"] = out_lang
        if response_format == "verbose_json":
            payload["task"] = "transcribe"
            payload["duration"] = round(pcm.duration_sec, 3)
            payload["segments"] = []
        # 不回 usage：本地推理没有可信的 token 账，编一个不如不报（主仓无 usage 时会自己估算
        # 并标 estimated:true）。
        self._send(200, payload)

    def _send_text(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def _short(exc: BaseException, limit: int = 300) -> str:
    return (str(exc) or type(exc).__name__)[:limit]


def _safe_token(s: str, limit: int = 60) -> str:
    """请求里的 model 字段只记录、不拒收；记之前压掉换行，别让它污染日志行。"""
    return "".join(ch for ch in s[:limit] if ch.isprintable()) or "-"


class ShimServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # 端口被占就直接失败，不要悄悄抢别人的监听

    def __init__(self, port: int, handler_cls):
        super().__init__((BIND_HOST, port), handler_cls)

    def server_bind(self) -> None:
        # 断言监听地址就是 127.0.0.1（测试会检查；也防以后有人手滑改 BIND_HOST）
        assert self.server_address[0] == BIND_HOST or BIND_HOST == "127.0.0.1"
        super().server_bind()
        host, _port = self.server_address[:2]
        if host not in ("127.0.0.1",):
            raise RuntimeError("拒绝在非 127.0.0.1 的地址上监听：" + str(host))


def build_server(settings: Settings, manager: EngineManager) -> ShimServer:
    handler = type("BoundShimHandler", (ShimHandler,), {"settings": settings, "manager": manager})
    return ShimServer(settings.port, handler)


def serve(settings: Settings | None = None) -> int:
    settings = settings or Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    audio_mod.cleanup_stale_temp()

    from .engine import make_backend

    def factory(model_name: str = ""):
        # auto 模式：点名某个尺寸 → 直接指到 models 目录里那一份；auto／空 → 让后端按 autopick 挑（缺省最省显存的那份）。
        if settings.auto:
            from .autopick import by_name, is_auto  # noqa: PLC0415

            cand = None if is_auto(model_name) else by_name(model_name)
            if cand is not None:
                return make_backend(model_repo=cand.repo, model_dir=os.path.join(settings.models_root, cand.dirname),
                                    device=settings.device, dtype=settings.dtype)
            return make_backend(model_repo="", model_dir="", device=settings.device, dtype=settings.dtype,
                                models_root=settings.models_root, prefer=settings.prefer)
        return make_backend(model_repo=settings.model_repo, model_dir=settings.model_dir,
                            device=settings.device, dtype=settings.dtype)

    manager = EngineManager(backend_factory=factory, idle_unload_sec=settings.idle_unload_sec,
                            default_model=settings.model_name)

    try:
        httpd = build_server(settings, manager)
    except OSError as exc:
        # 约定 §2.2：启动失败立刻非零退出，stderr 留一句人话，不要挂着重试。
        print(
            "ruyi-asr-shim 起不来：127.0.0.1:%d 这个端口用不了（%s）。\n"
            "  端口被别的程序占了就换一个：设环境变量 RUYI_ASR_PORT=<别的端口>，或加 --port 参数。\n"
            "  如意会自己挑空闲端口并经 RUYI_ASR_PORT 告诉本服务。"
            % (settings.port, exc),
            file=sys.stderr,
        )
        return 2

    LOG.info("ruyi-asr-shim %s 已监听 http://127.0.0.1:%d", __version__, settings.port)
    if settings.auto:
        LOG.info("模型：auto（models 目录 %s；可选 %s；auto 缺省挑%s），空闲 %d 秒卸载%s",
                 settings.models_root or "-", "/".join(m["id"] for m in settings.catalog()),
                 "最省显存的那份" if settings.prefer == "small" else "最大能装下的那份",
                 settings.idle_unload_sec, "（0 = 常驻）" if settings.idle_unload_sec == 0 else "")
    else:
        LOG.info("模型：%s（仓库 %s），懒加载，空闲 %d 秒卸载%s",
                 settings.model_name, settings.model_repo, settings.idle_unload_sec,
                 "（0 = 常驻）" if settings.idle_unload_sec == 0 else "")
    # 这里【故意不】去问 torch 有没有 CUDA：组件登记约定 §2.2 要求空转要轻，
    # 没收到第一发转写请求之前不许 import torch/transformers。设备会在首次加载时打印。
    LOG.info("推理设备在第一发转写请求时决定（有 CUDA 就用 CUDA，没有就 CPU）。"
             "想提前确认：python -m ruyi_asr_shim doctor")
    LOG.info("在如意里填的服务商地址：http://127.0.0.1:%d/v1 ，模型名：%s",
             settings.port, settings.model_name)

    # 父进程看门狗：如意崩了／被强杀时别留一个占着显存的孤儿。
    watchdog = watchdog_from_env(lambda: _stop_async(httpd))
    if watchdog is not None:
        LOG.info("父进程看门狗已启动，盯着 pid %d", watchdog.parent_pid)
        watchdog.start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("收到中断，正在退出…")
    finally:
        if watchdog is not None:
            watchdog.stop()
        try:
            httpd.shutdown()
        except Exception:
            pass
        httpd.server_close()
        manager.shutdown()
        audio_mod.cleanup_stale_temp()
    return 0


def _stop_async(httpd) -> None:
    """从别的线程停掉 serve_forever（shutdown() 不能在服务线程里调）。"""
    threading.Thread(target=httpd.shutdown, name="asr-stop", daemon=True).start()


def doctor() -> int:
    """显式的环境自检：这是唯一会主动 import torch 的入口（用户敲了才跑）。"""
    print("ruyi-asr-shim %s" % __version__)
    print("python: %s" % sys.version.split()[0])
    print("解释器: %s" % sys.executable)
    ok = True
    try:
        import torch  # noqa: PLC0415

        hip = getattr(torch.version, "hip", None)
        if hip:
            print("torch: %s（AMD ROCm/HIP 构建 %s）" % (torch.__version__, hip))
        else:
            print("torch: %s（CUDA 构建 %s）" % (torch.__version__, torch.version.cuda))

        # 用的是引擎同一套选择逻辑，省得「doctor 说能用、真跑起来是另一回事」。
        from .devices import build_probes, select_device  # noqa: PLC0415

        pref = os.environ.get("RUYI_ASR_DEVICE", "auto")
        dtype_pref = os.environ.get("RUYI_ASR_DTYPE", "auto")
        choice = select_device(pref, build_probes(torch, dtype_pref))
        print("会用的设备: %s" % choice.label)
        if choice.kind == "cpu":
            print("  —— 没有可用的显卡加速，推理会很慢。")
            print("  英伟达卡：装 cu128 及以上的 torch（scripts\\install.ps1 -Gpu nvidia）。")
            print("  AMD 卡：按 README「AMD 显卡」一节装 ROCm on Windows 的轮子。")
            ok = False
        elif choice.kind == "rocm":
            # ROCm 上 get_device_capability 回的是 (11, 0) 这种，印成 sm_110 是英伟达的写法、会误导；报 gfx 架构名。
            try:
                print("架构: %s" % torch.cuda.get_device_properties(0).gcnArchName)
            except Exception:
                pass
        elif choice.kind == "cuda":
            try:
                cap = torch.cuda.get_device_capability(0)
                print("算力: sm_%d%d" % cap)
            except Exception:
                pass
    except ImportError:
        print("torch: 没装。跑 scripts/install.ps1。")
        ok = False
    try:
        import transformers  # noqa: PLC0415

        print("transformers: %s（Qwen3-ASR 原生支持要 >= 5.13.0）" % transformers.__version__)
    except ImportError:
        print("transformers: 没装。跑 scripts/install.ps1。")
        ok = False
    try:
        import soundfile  # noqa: PLC0415

        print("soundfile: %s（libsndfile %s）"
              % (soundfile.__version__, getattr(soundfile, "__libsndfile_version__", "?")))
    except ImportError:
        print("soundfile: 没装 —— 只能解 PCM 的 wav（麦克风那一路够用）。")
    model_dir = os.environ.get("RUYI_ASR_MODEL_DIR", "").strip()
    if model_dir:
        print("模型目录: %s（%s）" % (model_dir, "在" if os.path.isdir(model_dir) else "不存在"))
    else:
        print("模型目录: 没设 RUYI_ASR_MODEL_DIR —— 会尝试联网下载。")
    from .registry import registration_path  # noqa: PLC0415

    rp = registration_path()
    print("登记文件: %s（%s）" % (rp, "已登记" if rp.exists() else "未登记"))
    return 0 if ok else 1


def pick_free_port() -> int:
    """测试用：拿一个空闲端口。"""
    s = socket.socket()
    try:
        s.bind((BIND_HOST, 0))
        return int(s.getsockname()[1])
    finally:
        s.close()
