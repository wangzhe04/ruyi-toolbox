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
    """一把锁串行；第一发请求才加载；空闲到点卸载。

    第 133 波：一个进程管多份模型（0.6B／1.7B／auto）—— 同一时刻只加载【一份】。请求带 model 且与已加载的不是同一份
    → 先卸掉再加载要的那份（显存里永远最多一份）；`unload_now()` 给如意「用户切走了 → 立刻释放显存」用。
    工厂 `backend_factory(model_name)` 按名造后端；老式无参工厂照样收（测试与单模型模式）。
    """

    def __init__(
        self,
        backend_factory: Callable[..., Backend],
        idle_unload_sec: int = 600,
        *,
        clock: Callable[[], float] = time.monotonic,
        default_model: str = "",
    ):
        self._factory = backend_factory
        self._idle_sec = max(0, int(idle_unload_sec))
        self._clock = clock
        self._default_model = str(default_model or "")
        self._lock = threading.Lock()          # 串行闸：加载与推理共用
        self._state_lock = threading.Lock()    # 只保护下面这几个小字段
        self._backend: Backend | None = None
        self._last_used = clock()
        self._load_count = 0
        self._unload_count = 0
        self._device = "unknown"
        self._model = ""                        # 加载后后端报的实际那份（auto 模式下才知道挑了哪份）
        self._requested = ""                    # 这份后端是按哪个名字造的（"" / auto / qwen3-asr-0.6b …）
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    # ── 只读状态：绝不触发加载（/health 判据）────────────────────────────

    def status(self) -> dict:
        with self._state_lock:
            return {
                "loaded": self._backend is not None,
                "device": self._device,
                "model": self._model,
                "requested": self._requested,
                "loadCount": self._load_count,
                "unloadCount": self._unload_count,
                "idleUnloadSec": self._idle_sec,
            }

    def _make(self, model: str) -> Backend:
        """按名造后端；工厂不收参数（老式）就裸调。"""
        try:
            return self._factory(model)
        except TypeError as exc:
            if "positional argument" not in str(exc) and "takes 0" not in str(exc):
                raise
            return self._factory()

    def _same_model(self, requested: str) -> bool:
        """已加载的那份能不能直接服务这个请求：名字相同，或请求的正是 auto 实际挑中的那份。"""
        if not requested or requested == self._requested:
            return True
        return bool(self._model) and requested == self._model

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

    def transcribe(self, pcm: AudioBuffer, language: str | None = None, prompt: str | None = None,
                   model: str | None = None) -> dict:
        # 锁住整段：加载与推理都在里面。`with` 保证抛异常时锁一定被放开（判据之一）。
        with self._lock:
            backend = self._ensure_loaded(str(model or ""))
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

    def _ensure_loaded(self, requested: str = "") -> Backend:
        """调用方已持有 self._lock。"""
        requested = requested or self._default_model
        with self._state_lock:
            if self._backend is not None and self._same_model(requested):
                return self._backend
            stale = self._backend
            self._backend = None
        if stale is not None:
            # 换模型：先把旧的卸干净（显存里最多一份），再加载新的。
            with self._state_lock:
                self._unload_count += 1
                self._device = "unknown"
            self._do_unload(stale)
            LOG.info("换模型：已卸载 %s，改加载 %s", self._model or self._requested or "-", requested or "-")
        t0 = self._clock()
        try:
            backend = self._make(requested)
            backend.load()
        except UnsupportedAudioError:
            raise
        except Exception as exc:
            LOG.error("模型加载失败：%s", str(exc)[:400])
            raise EngineError("模型加载失败：" + (str(exc)[:400] or type(exc).__name__)) from exc
        with self._state_lock:
            self._backend = backend
            self._requested = requested
            self._load_count += 1
            self._device = getattr(backend, "device", "unknown")
            self._model = str(getattr(backend, "resolved_model", "") or "")
            self._last_used = self._clock()
        LOG.info("模型已加载 device=%s model=%s load_ms=%d", self._device, self._model or "-", int((self._clock() - t0) * 1000))
        self._start_reaper()
        return backend

    def unload_now(self) -> bool:
        """立刻卸载（如意：用户把语音识别切走了）。等在途的那一发转完再卸；没加载回 False。"""
        with self._lock:
            with self._state_lock:
                backend = self._backend
                if backend is None:
                    return False
                self._backend = None
                self._unload_count += 1
                self._device = "unknown"
                name = self._model or self._requested
            self._do_unload(backend)
            LOG.info("按请求卸载模型 %s，已释放显存", name or "-")
            return True

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
                 dtype: str = "auto", models_root: str = "", prefer: str = "small") -> Backend:
    from .qwen_backend import Qwen3AsrBackend

    return Qwen3AsrBackend(model_repo=model_repo, model_dir=model_dir,
                           device=device, dtype=dtype, models_root=models_root, prefer=prefer)
