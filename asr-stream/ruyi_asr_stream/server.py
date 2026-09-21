"""HTTP 层：标准库 http.server，只绑 127.0.0.1，「有会话的 HTTP」流式识别（方案 02 §2）。

路由：
  GET    /health                          进程活着即 200（模型启动时已加载，探针不做重活）
  POST   /v1/stream/sessions              开会话 → {id, sampleRate}；体可带 {hotwords:[...]}
  POST   /v1/stream/sessions/{id}/audio   喂一块 16 kHz PCM16LE（≤ 1 MB）→ {partial, finals}
  POST   /v1/stream/sessions/{id}/finish  冲尾巴、关会话 → {finals}
  DELETE /v1/stream/sessions/{id}         关会话 → 204
  GET    /v1/models                       列出本进程提供的模型（流式一个，配了离线再加一个）
  POST   /v1/audio/transcriptions         131c：离线整句识别（SenseVoice，OpenAI 形 multipart，只认 WAV）
                                          没配离线模型 → 409 offline_not_configured

安全闸与 asr-shim 一字不差：只绑 127.0.0.1；Host 必须是 127.0.0.1:<port>/localhost:<port>；带 Origin 一律 403；不回 CORS 头。
日志只记元数据：会话数、字节数、耗时、文本长度 —— 不记文本。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import COMPONENT_NAME_TAG, DEFAULT_MODEL_NAME, DEFAULT_OFFLINE_MODEL_NAME, __version__
from .audio import UnsupportedAudioError, decode_wav_to_mono16k
from .engine import OfflineTranscriber, SessionLimit, SessionManager, UnknownSession
from .multipart import MultipartError, boundary_from_content_type, field_value, find_part, parse_multipart
from .watchdog import watchdog_from_env

LOG = logging.getLogger("ruyi_asr_stream")

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
MAX_CHUNK_BYTES = 1024 * 1024          # 单块 ≤ 1 MB（32 s 音频；正常是 250 ms = 8 KB）
MAX_JSON_BYTES = 64 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024     # 离线整句识别的 multipart 上限（与 asr-shim、如意的 25 MB 同口径）
MAX_FIELD = 4096
SESSION_RE = re.compile(r"^/v1/stream/sessions/([0-9a-f]{32})(?:/(audio|finish))?$")


class Settings:
    """全部可配项只走环境变量／命令行；绝不读写如意的数据目录。"""

    def __init__(self, argv: list[str] | None = None):
        env = os.environ
        ap = argparse.ArgumentParser(prog="python -m ruyi_asr_stream", add_help=True)
        ap.add_argument("--port", type=int, default=_env_int(env, "RUYI_ASR_STREAM_PORT", DEFAULT_PORT))
        ap.add_argument("--model-dir", default=env.get("RUYI_ASR_STREAM_MODEL_DIR", "").strip())
        ap.add_argument("--model", default=env.get("RUYI_ASR_STREAM_MODEL", DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME)
        ap.add_argument("--threads", type=int, default=_env_int(env, "RUYI_ASR_STREAM_THREADS", 2))
        ap.add_argument("--rule1-sec", type=float, default=_env_float(env, "RUYI_ASR_STREAM_RULE1_SEC", 2.0))
        ap.add_argument("--rule2-sec", type=float, default=_env_float(env, "RUYI_ASR_STREAM_RULE2_SEC", 0.8))
        ap.add_argument("--rule3-sec", type=float, default=_env_float(env, "RUYI_ASR_STREAM_RULE3_SEC", 20.0))
        ap.add_argument("--hotwords-file", default=env.get("RUYI_ASR_STREAM_HOTWORDS_FILE", "").strip())
        ap.add_argument("--max-sessions", type=int, default=_env_int(env, "RUYI_ASR_STREAM_MAX_SESSIONS", 4))
        ap.add_argument("--idle-sec", type=float, default=_env_float(env, "RUYI_ASR_STREAM_IDLE_SEC", 30.0))
        ap.add_argument("--log-level", default=env.get("RUYI_ASR_STREAM_LOG_LEVEL", "INFO").strip() or "INFO")
        # 131a：解码方式，缺省 modified_beam_search（见 engine.normalize_decoding）
        ap.add_argument("--decoding", default=env.get("RUYI_ASR_STREAM_DECODING", "").strip())
        # 131c：离线整句识别（SenseVoice）。不给目录 = 不开这条路（/v1/audio/transcriptions 回 409）
        ap.add_argument("--offline-model-dir", default=env.get("RUYI_ASR_STREAM_OFFLINE_MODEL_DIR", "").strip())
        ap.add_argument("--offline-model", default=env.get("RUYI_ASR_STREAM_OFFLINE_MODEL", "").strip() or DEFAULT_OFFLINE_MODEL_NAME)
        ap.add_argument("--offline-threads", type=int, default=_env_int(env, "RUYI_ASR_STREAM_OFFLINE_THREADS", 2))
        ns = ap.parse_args([] if argv is None else argv)
        self.port = int(ns.port)
        self.model_dir = ns.model_dir
        self.model_name = ns.model
        self.decoding = ns.decoding
        self.offline_model_dir = ns.offline_model_dir
        self.offline_model_name = ns.offline_model
        self.offline_threads = max(1, int(ns.offline_threads))
        self.threads = max(1, int(ns.threads))
        self.rule1_sec = float(ns.rule1_sec)
        self.rule2_sec = float(ns.rule2_sec)
        self.rule3_sec = float(ns.rule3_sec)
        self.hotwords_file = ns.hotwords_file
        self.max_sessions = max(1, int(ns.max_sessions))
        self.idle_sec = max(1.0, float(ns.idle_sec))
        self.log_level = ns.log_level


def _env_int(env, key: str, default: int) -> int:
    raw = str(env.get(key, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(env, key: str, default: float) -> float:
    raw = str(env.get(key, "")).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class StreamHandler(BaseHTTPRequestHandler):
    server_version = "ruyi-asr-stream"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    settings: Settings
    manager: SessionManager
    offline: OfflineTranscriber | None = None

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        LOG.debug("http %s", fmt % args)

    # ── 基础设施 ───────────────────────────────────────────────────────────

    def _send(self, status: int, payload: dict | None, *, close: bool = False) -> None:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _fail(self, status: int, err_type: str, message: str, *, close: bool = False) -> None:
        self._send(status, {"error": {"message": message, "type": err_type}}, close=close)

    def _gate(self) -> bool:
        if self.headers.get("Origin") is not None:
            LOG.warning("gate reject: origin header present")
            self._fail(403, "forbidden", "本服务只服务本机程序，不接受浏览器跨站请求（带 Origin 头一律拒绝）。", close=True)
            return False
        host = str(self.headers.get("Host") or "").strip().lower()
        if host not in {f"127.0.0.1:{self.settings.port}", f"localhost:{self.settings.port}"}:
            LOG.warning("gate reject: bad host header")
            self._fail(403, "forbidden", "Host 头必须是 127.0.0.1:%d 或 localhost:%d（防 DNS rebinding）。"
                       % (self.settings.port, self.settings.port), close=True)
            return False
        return True

    def _path(self) -> str:
        raw = self.path or "/"
        return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/"

    def _read_body(self, limit: int) -> bytes | None:
        """读定长请求体；超限先回 413 再断连（不读完对方的洪水）。回 None 表示已经回过错误。"""
        raw_len = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self._fail(411, "length_required", "请带 Content-Length（不支持 chunked）。", close=True)
            return None
        try:
            length = int(raw_len or 0)
        except ValueError:
            self._fail(400, "bad_request", "Content-Length 不是数字。", close=True)
            return None
        if length < 0:
            self._fail(400, "bad_request", "Content-Length 为负。", close=True)
            return None
        if length > limit:
            # 先把对方已经在路上的字节吞掉再回 413：不吞的话服务端一关连接，Windows 上客户端先收到的是
            # 「连接被中止」而不是 413（asr-shim 那边实测同样的坑）。吞的上限 8 MB —— 真洪水就直接断。
            remain = length
            while remain > 0 and length <= 8 * 1024 * 1024:
                chunk = self.rfile.read(min(65536, remain))
                if not chunk:
                    break
                remain -= len(chunk)
            self._fail(413, "payload_too_large", "请求体超过 %d 字节上限。" % limit, close=True)
            return None
        data = b""
        while len(data) < length:
            chunk = self.rfile.read(length - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) != length:
            self._fail(400, "bad_request", "请求体不完整。", close=True)
            return None
        return data

    # ── 路由 ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        if not self._gate():
            return
        path = self._path()
        if path == "/health":
            return self._health()
        if path == "/v1/models":
            return self._models()
        return self._fail(404, "not_found", "没有这条路径。本服务只有 /health、/v1/models、/v1/stream/sessions… 与 /v1/audio/transcriptions。")

    def do_POST(self) -> None:  # noqa: N802
        if not self._gate():
            return
        path = self._path()
        if path == "/v1/stream/sessions":
            return self._open()
        m = SESSION_RE.match(path)
        if m and m.group(2) == "audio":
            return self._audio(m.group(1))
        if m and m.group(2) == "finish":
            return self._finish(m.group(1))
        if path == "/v1/audio/transcriptions":
            return self._transcriptions()
        if path in ("/health", "/v1/models"):
            return self._fail(405, "method_not_allowed", "该端点只接受 GET。", close=True)
        return self._fail(404, "not_found", "没有这条路径。本服务只有 /health、/v1/models、/v1/stream/sessions… 与 /v1/audio/transcriptions。", close=True)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._gate():
            return
        m = SESSION_RE.match(self._path())
        if m and m.group(2) is None:
            if self.manager.close(m.group(1), reason="delete"):
                return self._send(204, None)
            return self._fail(404, "unknown_session", "没有这个会话（可能已超时关闭）。")
        return self._fail(404, "not_found", "没有这条路径。", close=True)

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._gate():
            return
        self._fail(405, "method_not_allowed", "本服务不支持 HEAD。")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._gate():
            return
        self._fail(405, "method_not_allowed", "本服务只接受 GET、POST 与 DELETE。", close=True)

    do_PATCH = do_PUT
    do_OPTIONS = do_HEAD  # 不做 CORS 预检

    # ── 处理器 ─────────────────────────────────────────────────────────────

    def _health(self) -> None:
        self._send(200, {
            "ok": True,
            "component": COMPONENT_NAME_TAG,
            "version": __version__,
            "loaded": True,
            "sessions": self.manager.count(),
            "model": self.settings.model_name,
            "backend": getattr(self.manager.backend, "name", "?"),
            "decoding": getattr(self.manager.backend, "decoding", "?"),
            # 131c：离线整句识别有没有配。null = 没配；配了就报模型名与已处理句数（不记文本）。
            "offline": ({"model": self.offline.model_name, "backend": getattr(self.offline.backend, "name", "?"), "count": self.offline.count}
                        if self.offline is not None else None),
        })

    def _models(self) -> None:
        data = [{"id": self.settings.model_name, "object": "model", "created": 0, "owned_by": COMPONENT_NAME_TAG, "capabilities": ["asr-stream"]}]
        if self.offline is not None:
            data.append({"id": self.offline.model_name, "object": "model", "created": 0, "owned_by": COMPONENT_NAME_TAG, "capabilities": ["asr"]})
        self._send(200, {"object": "list", "data": data})

    def _transcriptions(self) -> None:
        """131c：OpenAI 形 /v1/audio/transcriptions（multipart：file 必填，model / response_format 可选）。只认 WAV。"""
        if self.offline is None:
            return self._fail(409, "offline_not_configured",
                              "本组件没配离线整句识别模型。跑 scripts\\download-model.ps1（缺省会一并下载 SenseVoice）再登记一次。")
        ctype = str(self.headers.get("Content-Type") or "")
        if ctype.split(";")[0].strip().lower() != "multipart/form-data":
            return self._fail(415, "unsupported_media_type", "本端点只接受 multipart/form-data（OpenAI /v1/audio/transcriptions 形）。")
        body = self._read_body(MAX_AUDIO_BYTES)
        if body is None:
            return
        try:
            parts = parse_multipart(body, boundary_from_content_type(ctype))
        except MultipartError as exc:
            return self._fail(400, "bad_request", "multipart 解析失败：" + _short(exc))
        file_part = find_part(parts, "file")
        if file_part is None:
            return self._fail(400, "bad_request", "缺少 file 字段（OpenAI 形的音频文件段）。")
        if not file_part.data:
            return self._fail(400, "bad_request", "file 段是空的。")
        response_format = (field_value(parts, "response_format", 64).strip() or "json").lower()
        if response_format not in ("json", "verbose_json", "text"):
            return self._fail(400, "bad_request", "response_format 只支持 json / verbose_json / text。")
        try:
            pcm = decode_wav_to_mono16k(file_part.data)
        except UnsupportedAudioError as exc:
            LOG.info("offline reject: unsupported audio bytes=%d", len(file_part.data))
            return self._fail(415, "unsupported_media_type", str(exc))
        try:
            out = self.offline.transcribe(pcm.samples)
        except Exception as exc:  # noqa: BLE001 - 解码异常不该带垮服务
            LOG.exception("offline transcribe failed")
            return self._fail(500, "decode_failed", "识别出错：%s" % _short(exc))
        text = str(out.get("text") or "")
        if response_format == "text":
            raw = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            return
        payload: dict = {"text": text}
        if out.get("language"):
            payload["language"] = out["language"]
        if response_format == "verbose_json":
            payload["task"] = "transcribe"
            payload["duration"] = round(pcm.duration_sec, 3)
            payload["segments"] = []
        self._send(200, payload)

    def _open(self) -> None:
        body = self._read_body(MAX_JSON_BYTES)
        if body is None:
            return
        hotwords: list[str] = []
        if body.strip():
            try:
                obj = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._fail(400, "bad_request", "请求体不是合法 JSON。")
            raw = obj.get("hotwords") if isinstance(obj, dict) else None
            if raw is not None and not isinstance(raw, list):
                return self._fail(400, "bad_request", "hotwords 必须是字符串数组。")
            hotwords = [w for w in (raw or []) if isinstance(w, str)]
        try:
            sess = self.manager.open(hotwords)
        except SessionLimit as exc:
            return self._fail(429, "too_many_sessions", str(exc) + "。先把不用的会话 finish/DELETE 掉。")
        self._send(200, {"id": sess.id, "sampleRate": 16000})

    def _audio(self, sid: str) -> None:
        ctype = str(self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("audio/l16", "application/octet-stream", ""):
            return self._fail(415, "unsupported_media_type", "音频块的 Content-Type 须是 audio/L16; rate=16000（16 kHz 单声道 PCM16LE）。")
        body = self._read_body(MAX_CHUNK_BYTES)
        if body is None:
            return
        if len(body) % 2:
            return self._fail(400, "bad_request", "PCM16 字节数必须是偶数。")
        try:
            out = self.manager.feed(sid, body)
        except UnknownSession:
            return self._fail(404, "unknown_session", "没有这个会话（可能已超时关闭，重新开一个）。")
        except Exception as exc:  # noqa: BLE001 - 解码异常不该带垮服务
            LOG.exception("feed failed id=%s", sid[:8])
            return self._fail(500, "decode_failed", "识别出错：%s" % _short(exc))
        self._send(200, out)

    def _finish(self, sid: str) -> None:
        body = self._read_body(MAX_JSON_BYTES)
        if body is None:
            return
        try:
            out = self.manager.finish(sid)
        except UnknownSession:
            return self._fail(404, "unknown_session", "没有这个会话（可能已超时关闭）。")
        except Exception as exc:  # noqa: BLE001
            LOG.exception("finish failed id=%s", sid[:8])
            return self._fail(500, "decode_failed", "识别出错：%s" % _short(exc))
        self._send(200, out)


def _short(exc: BaseException, limit: int = 300) -> str:
    return (str(exc) or type(exc).__name__)[:limit]


class StreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, handler_cls):
        super().__init__((BIND_HOST, port), handler_cls)

    def server_bind(self) -> None:
        super().server_bind()
        host = self.server_address[0]
        if host != "127.0.0.1":
            raise RuntimeError("拒绝在非 127.0.0.1 的地址上监听：" + str(host))


def build_server(settings: Settings, manager: SessionManager, offline: OfflineTranscriber | None = None) -> StreamServer:
    handler = type("BoundStreamHandler", (StreamHandler,), {"settings": settings, "manager": manager, "offline": offline})
    return StreamServer(settings.port, handler)


def make_manager(settings: Settings) -> SessionManager:
    from .engine import SherpaBackend  # noqa: PLC0415

    backend = SherpaBackend(
        settings.model_dir, num_threads=settings.threads, rule1_sec=settings.rule1_sec,
        rule2_sec=settings.rule2_sec, rule3_sec=settings.rule3_sec, hotwords_file=settings.hotwords_file,
        decoding=settings.decoding,
    )
    return SessionManager(backend, max_sessions=settings.max_sessions, idle_sec=settings.idle_sec)


def make_offline(settings: Settings) -> OfflineTranscriber | None:
    if not settings.offline_model_dir:
        return None
    from .engine import SenseVoiceBackend  # noqa: PLC0415

    backend = SenseVoiceBackend(settings.offline_model_dir, num_threads=settings.offline_threads, model_name=settings.offline_model_name)
    return OfflineTranscriber(backend)


def serve(settings: Settings | None = None, manager: SessionManager | None = None, offline: OfflineTranscriber | None = None) -> int:
    settings = settings or Settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if manager is None:
        if not settings.model_dir:
            print("ruyi-asr-stream 起不来：没给模型目录。设 RUYI_ASR_STREAM_MODEL_DIR 或加 --model-dir。", file=sys.stderr)
            return 2
        try:
            manager = make_manager(settings)
        except Exception as exc:  # noqa: BLE001
            print("ruyi-asr-stream 起不来：模型加载失败（%s）。\n  模型目录：%s\n  先跑 scripts\\download-model.ps1。"
                  % (_short(exc), settings.model_dir), file=sys.stderr)
            return 3
    if offline is None and settings.offline_model_dir:
        # 离线模型坏了不该把流式那条路一起拖死：记一行、照常起，/v1/audio/transcriptions 回 409。
        try:
            offline = make_offline(settings)
        except Exception as exc:  # noqa: BLE001
            LOG.error("离线整句识别模型加载失败（%s），本次不开这条路。目录：%s", _short(exc), settings.offline_model_dir)
            offline = None
    try:
        httpd = build_server(settings, manager, offline)
    except OSError as exc:
        print("ruyi-asr-stream 起不来：127.0.0.1:%d 这个端口用不了（%s）。\n"
              "  端口被别的程序占了就换一个：设 RUYI_ASR_STREAM_PORT=<别的端口>，或加 --port。\n"
              "  如意会自己挑空闲端口并经 RUYI_ASR_STREAM_PORT 告诉本服务。" % (settings.port, exc), file=sys.stderr)
        return 2
    LOG.info("ruyi-asr-stream %s 已监听 http://127.0.0.1:%d", __version__, settings.port)
    LOG.info("模型：%s（%s），启动即加载、常驻；解码 %s；端点规则 rule1=%.1fs rule2=%.1fs rule3=%.0fs；线程 %d",
             settings.model_name, settings.model_dir or "-", getattr(manager.backend, "decoding", "?"),
             settings.rule1_sec, settings.rule2_sec, settings.rule3_sec, settings.threads)
    if offline is not None:
        LOG.info("离线整句识别：%s（%s），/v1/audio/transcriptions 已开", offline.model_name, settings.offline_model_dir)
    else:
        LOG.info("离线整句识别：未配（/v1/audio/transcriptions 回 409）。想要句尾改错不靠显卡：download-model.ps1 缺省会下 SenseVoice")

    watchdog = watchdog_from_env(lambda: _stop_async(httpd))
    if watchdog is not None:
        LOG.info("父进程看门狗已启动，盯着 pid %d", watchdog.parent_pid)
        watchdog.start()
    reaper_stop = threading.Event()

    def reaper():
        while not reaper_stop.wait(5.0):
            try:
                manager.reap()
            except Exception:  # noqa: BLE001
                LOG.exception("reap failed")

    threading.Thread(target=reaper, name="asr-stream-reaper", daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("收到中断，正在退出…")
    finally:
        reaper_stop.set()
        if watchdog is not None:
            watchdog.stop()
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        httpd.server_close()
        manager.shutdown()
    return 0


def _stop_async(httpd) -> None:
    threading.Thread(target=httpd.shutdown, name="asr-stream-stop", daemon=True).start()


def doctor() -> int:
    print("ruyi-asr-stream %s" % __version__)
    print("python: %s" % sys.version.split()[0])
    print("解释器: %s" % sys.executable)
    ok = True
    try:
        import sherpa_onnx  # noqa: PLC0415

        print("sherpa-onnx: %s" % getattr(sherpa_onnx, "__version__", "?"))
    except ImportError:
        print("sherpa-onnx: 没装。跑 scripts/install.ps1。")
        ok = False
    model_dir = os.environ.get("RUYI_ASR_STREAM_MODEL_DIR", "").strip()
    if model_dir:
        try:
            from .engine import resolve_model_files  # noqa: PLC0415

            files = resolve_model_files(model_dir)
            print("模型目录: %s" % model_dir)
            for k in ("encoder", "decoder", "joiner"):
                print("  %s: %s (%.0f MB)" % (k, os.path.basename(files[k]), os.path.getsize(files[k]) / 1e6))
        except FileNotFoundError as exc:
            print("模型目录: %s" % exc)
            ok = False
    else:
        print("模型目录: 没设 RUYI_ASR_STREAM_MODEL_DIR。跑 scripts/download-model.ps1。")
        ok = False
    offline_dir = os.environ.get("RUYI_ASR_STREAM_OFFLINE_MODEL_DIR", "").strip()
    if offline_dir:
        try:
            from .engine import resolve_offline_model_files  # noqa: PLC0415

            f = resolve_offline_model_files(offline_dir)
            print("离线整句识别: %s (%.0f MB)" % (f["model"], os.path.getsize(f["model"]) / 1e6))
        except FileNotFoundError as exc:
            print("离线整句识别: %s" % exc)
            ok = False
    else:
        print("离线整句识别: 没设 RUYI_ASR_STREAM_OFFLINE_MODEL_DIR（可选；没有它就没有不靠显卡的句尾改错）。")
    from .registry import registration_path  # noqa: PLC0415

    rp = registration_path()
    print("登记文件: %s（%s）" % (rp, "已登记" if rp.exists() else "未登记"))
    return 0 if ok else 1


def pick_free_port() -> int:
    s = socket.socket()
    try:
        s.bind((BIND_HOST, 0))
        return int(s.getsockname()[1])
    finally:
        s.close()
