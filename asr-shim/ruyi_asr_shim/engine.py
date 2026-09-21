"""引擎：生命周期管理（懒加载 / 串行 / 空闲卸载）＋ Qwen3-ASR 的 transformers 实现。

分两层是为了能测：
  - `EngineManager` 只管生命周期，对后端只要求三个方法（load / transcribe / unload），
    测试注入一个假后端就能把「懒加载只加载一次、两发请求串行、空闲到点卸载、异常后锁要放开」
    这些判据全部跑到，不需要 GPU、不需要下模型。
  - `Qwen3AsrBackend` 才碰 torch / transformers，import 也推迟到真要加载时才做
    （否则 `/health` 会被 import torch 拖上好几秒，而且没装 torch 的机器连服务都起不来）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol

from .audio import TARGET_SR, AudioBuffer, UnsupportedAudioError  # noqa: F401 - 对外再导出

LOG = logging.getLogger("ruyi_asr_shim.engine")


class EngineError(Exception):
    """后端加载或推理失败（回 500）。"""


class Backend(Protocol):
    device: str

    def load(self) -> None: ...

    def transcribe(self, pcm: AudioBuffer, language: str | None, prompt: str | None) -> dict: ...

    def unload(self) -> None: ...


class EngineManager:
    """一把锁串行；第一发请求才加载；空闲到点卸载。"""

    def __init__(
        self,
        backend_factory: Callable[[], Backend],
        idle_unload_sec: int = 600,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._factory = backend_factory
        self._idle_sec = max(0, int(idle_unload_sec))
        self._clock = clock
        self._lock = threading.Lock()          # 串行闸：加载与推理共用
        self._state_lock = threading.Lock()    # 只保护下面这几个小字段
        self._backend: Backend | None = None
        self._last_used = clock()
        self._load_count = 0
        self._unload_count = 0
        self._device = "unknown"
        self._model = ""                        # auto 模式下加载后才知道挑了哪份
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    # ── 只读状态：绝不触发加载（/health 判据）────────────────────────────

    def status(self) -> dict:
        with self._state_lock:
            return {
                "loaded": self._backend is not None,
                "device": self._device,
                "model": self._model,
                "loadCount": self._load_count,
                "unloadCount": self._unload_count,
                "idleUnloadSec": self._idle_sec,
            }

    @property
    def load_count(self) -> int:
        with self._state_lock:
            return self._load_count

    @property
    def unload_count(self) -> int:
        with self._state_lock:
            return self._unload_count

    @property
    def loaded(self) -> bool:
        with self._state_lock:
            return self._backend is not None

    # 这里【故意没有】探测设备的方法：组件登记约定 §2.2 要求空转要轻，
    # 第一发转写请求之前不许 import torch。想提前看环境请用 `python -m ruyi_asr_shim doctor`。

    # ── 主路径 ────────────────────────────────────────────────────────────

    def transcribe(self, pcm: AudioBuffer, language: str | None = None, prompt: str | None = None) -> dict:
        # 锁住整段：加载与推理都在里面。`with` 保证抛异常时锁一定被放开（判据之一）。
        with self._lock:
            backend = self._ensure_loaded()
            try:
                result = backend.transcribe(pcm, language, prompt)
            except UnsupportedAudioError:
                raise
            except EngineError:
                raise
            except Exception as exc:
                raise EngineError(str(exc)[:400] or type(exc).__name__) from exc
            finally:
                with self._state_lock:
                    self._last_used = self._clock()
        if not isinstance(result, dict) or not isinstance(result.get("text", ""), str):
            raise EngineError("后端回了一个形状不对的结果")
        return result

    def _ensure_loaded(self) -> Backend:
        with self._state_lock:
            if self._backend is not None:
                return self._backend
        t0 = self._clock()
        try:
            backend = self._factory()
            backend.load()
        except UnsupportedAudioError:
            raise
        except Exception as exc:
            LOG.error("模型加载失败：%s", str(exc)[:400])
            raise EngineError("模型加载失败：" + (str(exc)[:400] or type(exc).__name__)) from exc
        with self._state_lock:
            self._backend = backend
            self._load_count += 1
            self._device = getattr(backend, "device", "unknown")
            self._model = str(getattr(backend, "resolved_model", "") or "")
            self._last_used = self._clock()
        LOG.info("模型已加载 device=%s model=%s load_ms=%d", self._device, self._model or "-", int((self._clock() - t0) * 1000))
        self._start_reaper()
        return backend

    # ── 空闲卸载 ──────────────────────────────────────────────────────────

    def _start_reaper(self) -> None:
        if self._idle_sec <= 0:
            return  # 0 = 常驻，不起线程
        with self._state_lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap_loop, name="asr-idle-unload", daemon=True)
            th = self._reaper
        th.start()

    def _reap_loop(self) -> None:
        # 轮询间隔取空闲阈值的 1/4，夹在 [0.05s, 30s]：阈值调到 5 秒做验收时也能准时卸。
        interval = min(30.0, max(0.05, self._idle_sec / 4.0))
        while not self._stop.wait(interval):
            try:
                if self.maybe_unload_idle():
                    return  # 卸完就退线程；下次加载时会重新起
            except Exception:  # 守护线程绝不能把自己搞死
                LOG.exception("空闲卸载线程出错")

    def maybe_unload_idle(self) -> bool:
        """到点且不忙就卸载，回 True 表示确实卸了。忙的时候直接跳过，下一轮再说。"""
        if self._idle_sec <= 0:
            return False
        with self._state_lock:
            if self._backend is None:
                return False
            idle = self._clock() - self._last_used
        if idle < self._idle_sec:
            return False
        # 拿不到串行锁 = 正在推理，这轮放弃（绝不打断在途请求）。
        if not self._lock.acquire(blocking=False):
            return False
        try:
            with self._state_lock:
                if self._backend is None:
                    return False
                if self._clock() - self._last_used < self._idle_sec:
                    return False  # 刚被用过，收手
                backend = self._backend
                self._backend = None
                self._unload_count += 1
                self._device = "unknown"
            self._do_unload(backend)
            LOG.info("空闲 %d 秒，已卸载模型并释放显存", self._idle_sec)
            return True
        finally:
            self._lock.release()

    @staticmethod
    def _do_unload(backend: Backend) -> None:
        try:
            backend.unload()
        except Exception:
            LOG.exception("卸载后端时出错（忽略）")

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            with self._state_lock:
                backend = self._backend
                self._backend = None
                self._device = "unknown"
            if backend is not None:
                self._do_unload(backend)


# ── Qwen3-ASR 后端（真正碰 torch/transformers 的那一层）────────────────────
# 具体加载与调用形状见 asr-shim/docs/backend-notes.md（含查证来源与日期）。

def make_backend(model_repo: str, model_dir: str = "", device: str = "auto",
                 dtype: str = "auto", models_root: str = "") -> Backend:
    from .qwen_backend import Qwen3AsrBackend

    return Qwen3AsrBackend(model_repo=model_repo, model_dir=model_dir,
                           device=device, dtype=dtype, models_root=models_root)
