"""模型自动挑选（用户 2026-09-21 拍板：「如果有多个本地模型的话，默认用更大的（如果显存允许的话）」）。

登记时写 `RUYI_ASR_MODEL=auto` ＋ `RUYI_ASR_MODELS_ROOT=<models 目录>`；第一发转写请求触发加载时才决定用哪一份：
  · 有显卡：按空闲显存挑【装得下的最大】那份（需求量 = 权重 + 推理峰值 + 余量，实测见 52 号文 §6）；
  · 没显卡（CPU）：挑最小的（1.7B 在 CPU 上一句要十几秒，不值）；
  · 都装不下：仍挑最小的并记一行警告（退 CPU 那条路由 qwen_backend 自己兜）。
纯函数、不 import torch：空闲显存由调用方给（free_vram_mb），好让单元测试用假数字。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

LOG = logging.getLogger("ruyi_asr_shim.autopick")

AUTO_NAMES = ("auto", "qwen3-asr-auto")
AUTO_MODEL_NAME = "qwen3-asr-auto"   # 登记里 provides.model 用这个名；/health 另报 resolvedModel


@dataclass(frozen=True)
class Candidate:
    dirname: str
    name: str
    repo: str
    need_mb: int      # 载入 + 推理峰值 + 余量（fp16）。1.7B 实测载入 3.9 GB / 峰值 4.3 GB；0.6B 1.5 / 1.9 GB


# 大在前。加新尺寸只改这一张表。
CANDIDATES: tuple[Candidate, ...] = (
    Candidate("Qwen3-ASR-1.7B-hf", "qwen3-asr-1.7b", "Qwen/Qwen3-ASR-1.7B-hf", 4800),
    Candidate("Qwen3-ASR-0.6B-hf", "qwen3-asr-0.6b", "Qwen/Qwen3-ASR-0.6B-hf", 2200),
)


def is_auto(model_name: str) -> bool:
    return str(model_name or "").strip().lower() in AUTO_NAMES


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


def pick(models_root: str, free_mb: int | None) -> tuple[Candidate | None, str]:
    """回 (候选, 一句人话理由)。没有任何候选 → (None, 理由)。"""
    have = installed(models_root)
    if not have:
        return None, "models 目录里没有下全的 Qwen3-ASR：%s" % (models_root or "-")
    smallest = have[-1]
    if free_mb is None:
        return smallest, "没有显卡，用最小的 %s（CPU 上大模型太慢）" % smallest.name
    for c in have:
        if free_mb >= c.need_mb:
            return c, "空闲显存 %d MB ≥ %d MB，用最大能装下的 %s" % (free_mb, c.need_mb, c.name)
    return smallest, "空闲显存 %d MB 连最小的 %s（要 %d MB）都不够，仍用它（加载失败会退 CPU）" % (free_mb, smallest.name, smallest.need_mb)
