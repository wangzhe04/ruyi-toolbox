"""模型自动挑选与模型目录。

历史：用户 2026-09-21 上午拍板「有多个本地模型就默认用更大的（显存允许的话）」；同日下午改口——「1.7B 太占显存，
auto 默认 0.6B，用户在如意里自己切到 1.7B」。所以现在：
  · `auto`（如意里显示 qwen3-asr-auto）＝【最省显存的那份】（装了 0.6B 就是 0.6B；只装了 1.7B 就只能是它）；
    要更准的 1.7B 由用户在如意的语音设置里明确选 `qwen3-asr-1.7b`（第 133 波：每份都单列成一个模型名）。
  · 旧策略「最大能装下的」保留成 RUYI_ASR_AUTO_PREFER=large，不是缺省。
登记时写 `RUYI_ASR_MODEL=auto` ＋ `RUYI_ASR_MODELS_ROOT=<models 目录>`；挑哪份在加载时决定。
纯函数、不 import torch：空闲显存由调用方给（free_vram_mb），好让单元测试用假数字。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

LOG = logging.getLogger("ruyi_asr_shim.autopick")

AUTO_NAMES = ("auto", "qwen3-asr-auto")
AUTO_MODEL_NAME = "qwen3-asr-auto"   # 登记里 provides.model 用这个名；/health 另报 resolvedModel
AUTO_LABEL = "自动（先用 0.6B，省显存）"
PREFER_SMALL = "small"
PREFER_LARGE = "large"


@dataclass(frozen=True)
class Candidate:
    dirname: str
    name: str
    repo: str
    need_mb: int      # 载入 + 推理峰值 + 余量（fp16）。1.7B 实测载入 3.9 GB / 峰值 4.3 GB；0.6B 1.5 / 1.9 GB
    label: str        # 如意设置页里那一格显示的话（登记文件 provides.models[].label）


# 大在前。加新尺寸只改这一张表。
CANDIDATES: tuple[Candidate, ...] = (
    Candidate("Qwen3-ASR-1.7B-hf", "qwen3-asr-1.7b", "Qwen/Qwen3-ASR-1.7B-hf", 4800, "1.7B（更准，约 5 GB 显存）"),
    Candidate("Qwen3-ASR-0.6B-hf", "qwen3-asr-0.6b", "Qwen/Qwen3-ASR-0.6B-hf", 2200, "0.6B（约 2 GB 显存）"),
)


def is_auto(model_name: str) -> bool:
    return str(model_name or "").strip().lower() in AUTO_NAMES


def by_name(model_name: str) -> Candidate | None:
    key = str(model_name or "").strip().lower()
    for c in CANDIDATES:
        if c.name == key:
            return c
    return None


def normalize_prefer(raw: str) -> str:
    return PREFER_LARGE if str(raw or "").strip().lower() == PREFER_LARGE else PREFER_SMALL


def looks_installed(model_dir: str) -> bool:
    if not model_dir or not os.path.isdir(model_dir) or not os.path.isfile(os.path.join(model_dir, "config.json")):
        return False
    try:
        names = os.listdir(model_dir)
    except OSError:
        return False
    return any(n.endswith((".safetensors", ".bin")) and not n.endswith(".incomplete") for n in names)


def installed(models_root: str) -> list[Candidate]:
    """按大到小回 models_root 里下全了的候选。"""
    if not models_root or not os.path.isdir(models_root):
        return []
    return [c for c in CANDIDATES if looks_installed(os.path.join(models_root, c.dirname))]


def catalog(models_root: str) -> list[dict]:
    """如意能选的模型清单（登记文件 provides.models 与 /v1/models 同一份）：auto 在前，其后每份装好的按小到大。"""
    have = installed(models_root)
    out = [{"id": AUTO_MODEL_NAME, "label": AUTO_LABEL}]
    for c in reversed(have):
        out.append({"id": c.name, "label": c.label})
    return out


def free_vram_mb(torch) -> int | None:
    """有 torch.cuda 设备就回空闲显存 MB（CUDA 与 ROCm 同一接口），否则 None（= CPU）。绝不抛。"""
    try:
        if not hasattr(torch, "cuda") or not torch.cuda.is_available():
            return None
        free, _total = torch.cuda.mem_get_info()
        return int(free // (1024 * 1024))
    except Exception as exc:  # noqa: BLE001
        LOG.debug("mem_get_info 失败（当没显卡处理）：%s", str(exc)[:120])
        return None


def pick(models_root: str, free_mb: int | None, prefer: str = PREFER_SMALL) -> tuple[Candidate | None, str]:
    """回 (候选, 一句人话理由)。没有任何候选 → (None, 理由)。

    prefer=small（缺省）：装了几份都挑最小的；显存明显不够时也只能是它（加载失败会退 CPU）。
    prefer=large：按空闲显存挑最大能装下的（老策略）；没显卡挑最小的。
    """
    have = installed(models_root)
    if not have:
        return None, "models 目录里没有下全的 Qwen3-ASR：%s" % (models_root or "-")
    smallest = have[-1]
    if normalize_prefer(prefer) == PREFER_SMALL:
        if free_mb is not None and free_mb < smallest.need_mb:
            return smallest, "auto 挑最省显存的 %s；空闲显存 %d MB 比它要的 %d MB 少，仍用它（加载失败会退 CPU）" % (smallest.name, free_mb, smallest.need_mb)
        return smallest, "auto 挑最省显存的 %s（要更准的在如意语音设置里选 1.7B）" % smallest.name
    if free_mb is None:
        return smallest, "没有显卡，用最小的 %s（CPU 上大模型太慢）" % smallest.name
    for c in have:
        if free_mb >= c.need_mb:
            return c, "空闲显存 %d MB ≥ %d MB，用最大能装下的 %s" % (free_mb, c.need_mb, c.name)
    return smallest, "空闲显存 %d MB 连最小的 %s（要 %d MB）都不够，仍用它（加载失败会退 CPU）" % (free_mb, smallest.name, smallest.need_mb)
