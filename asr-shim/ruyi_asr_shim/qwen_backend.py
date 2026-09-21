"""Qwen3-ASR 的 transformers 后端。

【本文件所有调用形状的依据见 asr-shim/docs/backend-notes.md（含来源 URL 与查证日期）】要点：
- 用 `-hf` 那份仓库（`Qwen/Qwen3-ASR-0.6B-hf`）＋ `transformers>=5.13.0` 原生支持；
  非 `-hf` 的仓库是给官方 `qwen-asr` 包用的，config 结构不同，别拿去喂 AutoModel。
- 入口：`AutoProcessor.apply_transcription_request(...)` → `model.generate(...)`
  → `processor.decode(..., return_format="parsed")`。
- 传 ndarray 时 transformers **不做**重采样、不转单声道 —— 16 kHz 单声道 float32 由我们
  在 audio.py 里保证。这样也就完全不需要 ffmpeg／torchcodec。

import torch / transformers 全部压在 `load()` 里：进程空转时（如意一启动就把我们拉起来，
绝大多数时间没人说话）不许把这两个大家伙拉进内存。
"""

from __future__ import annotations

import gc
import inspect
import logging
import os
import time

import numpy as np

from .audio import TARGET_SR, AudioBuffer
from .devices import build_probes, probe_cpu, select_device

LOG = logging.getLogger("ruyi_asr_shim.qwen")

# 音频编码器的窗口是 30 秒（processor_config.json: chunk_length=30, n_samples=480000）。
# 超过就自己切窗口逐段转、把文本接起来；如意的麦克风一路是 2–30 秒，走不到这条分支。
DEFAULT_WINDOW_SEC = 30.0
WINDOW_MARGIN_SEC = 2.0  # 留点余量，别顶着上限喂


class Qwen3AsrBackend:
    def __init__(self, model_repo: str, model_dir: str = "", device: str = "auto",
                 dtype: str = "auto", models_root: str = ""):
        self.model_repo = model_repo
        self.model_dir = (model_dir or "").strip()
        # auto 模式：给 models_root、不给 model_dir；load() 里探完设备再按空闲显存挑（autopick.py）
        self.models_root = (models_root or "").strip()
        self.resolved_model = "" if self.models_root else _name_of(model_repo)
        self.device_pref = (device or "auto").strip().lower() or "auto"
        self.dtype_pref = (dtype or "auto").strip().lower() or "auto"
        self.device = "unknown"
        self._choice = None
        self._model = None
        self._processor = None
        self._torch = None
        self._window_sec = DEFAULT_WINDOW_SEC

    # ── 加载／卸载 ────────────────────────────────────────────────────────

    def _source(self) -> tuple[str, bool]:
        """回 (from_pretrained 的第一个参数, local_files_only)。"""
        if self.model_dir:
            if not os.path.isdir(self.model_dir):
                raise FileNotFoundError(
                    "模型目录不存在：%s（先跑 scripts/download-model.ps1）" % self.model_dir
                )
            return self.model_dir, True
        # 没给本地目录就按仓库名走在线下载（大陆直连 HF 可能不通，README 里说了怎么办）
        return self.model_repo, False

    def load(self) -> None:
        t0 = time.monotonic()
        import torch  # noqa: PLC0415 - 故意推迟到这里

        self._torch = torch

        # 设备选择走可插拔的那一层：一档坏了自动降到下一档，显卡后端坏掉也不至于整个服务起不来。
        # AMD 的 ROCm 构建就落在第一档（torch.cuda），不需要在这里分叉 —— 见 devices.py 的头注。
        choice = select_device(self.device_pref, build_probes(torch, self.dtype_pref))
        self._choice = choice

        if self.models_root:
            from .autopick import free_vram_mb, pick  # noqa: PLC0415

            free_mb = free_vram_mb(torch) if choice.device == "cuda" else None
            cand, why = pick(self.models_root, free_mb)
            if cand is None:
                raise FileNotFoundError(why + "（先跑 scripts/download-model.ps1）")
            self.model_dir = os.path.join(self.models_root, cand.dirname)
            self.model_repo = cand.repo
            self.resolved_model = cand.name
            LOG.info("自动选模型：%s —— %s", cand.name, why)

        from transformers import AutoProcessor  # noqa: PLC0415

        # 关掉 transformers 的进度条：日志走 stderr、会被如意接进它自己的日志，
        # tqdm 那一串 \r 回车在别人的日志里是一坨噪声。
        try:
            from transformers.utils import logging as hf_logging  # noqa: PLC0415

            hf_logging.disable_progress_bar()
        except Exception:
            LOG.debug("关不掉 transformers 进度条（忽略）", exc_info=True)

        model_cls = _load_model_class()
        src, local_only = self._source()

        LOG.info("正在加载模型 device=%s local_only=%s", choice.label, local_only)
        common = {"local_files_only": local_only} if local_only else {}
        self._processor = AutoProcessor.from_pretrained(src, **common)
        self._model = _from_pretrained_with_dtype(model_cls, src, dtype=choice.dtype, **common)
        try:
            self._model.to(choice.device)
        except Exception as exc:
            # 权重加载成功但搬不上加速器（显存不够、算子不支持……）：退 CPU，别让整条路断掉。
            LOG.warning("把模型搬到 %s 失败（%s），退回 CPU 继续跑", choice.kind, str(exc)[:200])
            self._choice = choice = probe_cpu(torch, self.dtype_pref)
            self._model = self._model.to("cpu").float()
        self._model.eval()

        self.device = choice.label
        self._window_sec = _window_seconds(self._processor)
        # 「模型已加载」那行由 EngineManager 统一打，这里只补它不知道的那一项。
        LOG.debug("音频窗口 %.0f 秒，加载耗时 %d ms",
                  self._window_sec, int((time.monotonic() - t0) * 1000))

    def unload(self) -> None:
        kind = self._choice.kind if self._choice is not None else ""
        self._model = None
        self._processor = None
        self._choice = None
        self.device = "unknown"
        gc.collect()
        torch = self._torch
        if torch is None:
            return
        # 各后端各自的「把显存还回去」。ROCm 走的也是 torch.cuda 这套。
        try:
            if kind in ("cuda", "rocm") and torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
            elif kind == "xpu" and getattr(torch, "xpu", None) is not None:
                torch.xpu.empty_cache()
            elif kind == "mps":
                torch.mps.empty_cache()
        except Exception:
            LOG.debug("empty_cache 失败（忽略）", exc_info=True)

    # ── 推理 ──────────────────────────────────────────────────────────────

    def transcribe(self, pcm: AudioBuffer, language: str | None, prompt: str | None) -> dict:
        if self._model is None or self._processor is None:
            raise RuntimeError("模型还没加载")
        if pcm.sample_rate != TARGET_SR:
            raise RuntimeError("内部错误：喂给模型的采样率不是 %d" % TARGET_SR)

        window = max(5.0, self._window_sec - WINDOW_MARGIN_SEC)
        max_samples = int(window * TARGET_SR)
        chunks = _split(pcm.samples, max_samples)
        if len(chunks) > 1:
            LOG.info("音频 %.1f 秒超过 %.0f 秒窗口，切成 %d 段顺序转写",
                     pcm.duration_sec, window, len(chunks))

        texts: list[str] = []
        out_lang = ""
        for chunk in chunks:
            text, lang = self._transcribe_one(chunk, language, prompt)
            if text:
                texts.append(text)
            if lang and not out_lang:
                out_lang = lang
        return {"text": _join(texts), "language": out_lang}

    def _transcribe_one(self, samples: np.ndarray, language: str | None, prompt: str | None):
        torch = self._torch
        processor, model = self._processor, self._model

        kwargs = {"audio": np.ascontiguousarray(samples, dtype=np.float32)}
        # 采样率的正确塞法（transformers 5.17 实测）：`processor_kwargs={"audio_kwargs": {...}}`。
        # 直接传顶层 `sampling_rate=`，或者传顶层 `audio_kwargs=`，都会换来一行
        # 「Kwargs passed to `processor.__call__` have to be in `processor_kwargs` dict」然后被吞掉。
        # 我们喂进去的本来就是 16 kHz（audio.py 保证），这里写死是为了不靠默认值。
        kwargs["processor_kwargs"] = {"audio_kwargs": {"sampling_rate": TARGET_SR}}
        if language:
            _maybe(kwargs, processor.apply_transcription_request, "language", language)
        if prompt:
            # transformers 路径把热词／领域词叫 prompt（qwen-asr 包里叫 context）。
            _maybe(kwargs, processor.apply_transcription_request, "prompt", prompt[:2000])

        try:
            inputs = processor.apply_transcription_request(**kwargs)
        except TypeError:
            # 上游哪天改了 processor_kwargs 的名字，不至于整条路断掉：去掉它再试一次
            # （我们喂的本来就是 16 kHz，processor_config.json 里的默认值也是 16000）。
            kwargs.pop("processor_kwargs", None)
            inputs = processor.apply_transcription_request(**kwargs)
        inputs = inputs.to(model.device, model.dtype)

        n_in = int(inputs["input_ids"].shape[1])
        seconds = len(samples) / float(TARGET_SR)
        max_new = int(min(1024, max(64, seconds * 20)))
        with torch.inference_mode():
            output_ids = model.generate(**inputs, max_new_tokens=max_new, do_sample=False)
        generated = output_ids[:, n_in:]

        return _decode(processor, generated)


# ── 一堆「别被上游小改动绊倒」的适配小工具 ────────────────────────────────


def _load_model_class():
    """优先 AutoModelForMultimodalLM（模型卡的写法），退到显式类名。"""
    import transformers  # noqa: PLC0415

    for name in ("AutoModelForMultimodalLM", "Qwen3ASRForConditionalGeneration"):
        cls = getattr(transformers, name, None)
        if cls is not None:
            return cls
    raise RuntimeError(
        "这个 transformers 版本里找不到 Qwen3-ASR 的模型类。"
        "原生支持从 transformers 5.13.0 起，请 pip install -U 'transformers>=5.13.0'。"
    )


def _from_pretrained_with_dtype(cls, src: str, dtype, **kw):
    """transformers v5 把 `torch_dtype` 改名成 `dtype`；两边都试一下。"""
    try:
        return cls.from_pretrained(src, dtype=dtype, **kw)
    except TypeError:
        return cls.from_pretrained(src, torch_dtype=dtype, **kw)


def _maybe(kwargs: dict, fn, name: str, value) -> None:
    """只在目标函数确实收这个参数时才塞进去（签名看不出来就塞，让它自己报错）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        kwargs[name] = value
        return
    params = sig.parameters
    if name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs[name] = value


def _decode(processor, generated) -> tuple[str, str]:
    """把生成的 token 解成 (文本, 语言)。

    首选 `return_format="parsed"`（模型卡的写法，回 {"language","transcription"}）；
    这个版本不认就退到 raw，再自己按 `language <NAME><asr_text>…` 的形状拆。
    """
    try:
        parsed = processor.decode(generated, return_format="parsed")
        item = parsed[0] if isinstance(parsed, (list, tuple)) else parsed
        if isinstance(item, dict):
            text = str(item.get("transcription") or item.get("text") or "")
            lang = str(item.get("language") or "")
            return text.strip(), lang.strip()
    except TypeError:
        pass
    except Exception:
        LOG.debug("parsed 解码失败，回退 raw", exc_info=True)

    raw = processor.decode(generated)
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    return _split_raw(str(raw))


def _split_raw(raw: str) -> tuple[str, str]:
    marker = "<asr_text>"
    lang = ""
    text = raw
    if marker in raw:
        head, text = raw.split(marker, 1)
        head = head.strip()
        if head.lower().startswith("language"):
            lang = head[len("language"):].strip()
        else:
            lang = head
    # 去掉可能残留的特殊 token
    for tok in ("<|im_end|>", "<|endoftext|>", "</s>"):
        text = text.replace(tok, "")
    return text.strip(), lang.strip()[:40]


def _name_of(model_repo: str) -> str:
    """仓库名 → 如意里的模型名（Qwen/Qwen3-ASR-1.7B-hf → qwen3-asr-1.7b）；认不出就原样。"""
    leaf = str(model_repo or "").rstrip("/").split("/")[-1]
    if leaf.lower().endswith("-hf"):
        leaf = leaf[:-3]
    return leaf.lower()


def _window_seconds(processor) -> float:
    """从 feature extractor 读窗口长度；读不到就用 30 秒。"""
    fe = getattr(processor, "feature_extractor", None)
    for attr in ("chunk_length",):
        val = getattr(fe, attr, None)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    n_samples = getattr(fe, "n_samples", None)
    sr = getattr(fe, "sampling_rate", TARGET_SR) or TARGET_SR
    if isinstance(n_samples, (int, float)) and n_samples > 0:
        return float(n_samples) / float(sr)
    return DEFAULT_WINDOW_SEC


def _split(x: np.ndarray, max_samples: int) -> list[np.ndarray]:
    if max_samples <= 0 or len(x) <= max_samples:
        return [x]
    return [x[i : i + max_samples] for i in range(0, len(x), max_samples)]


def _join(texts: list[str]) -> str:
    """接多段文本：中文之间不加空格，其余按空格接。"""
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if not out:
            out = t
            continue
        if _is_cjk(out[-1]) or _is_cjk(t[0]):
            out += t
        else:
            out += " " + t
    return out


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3000 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF
