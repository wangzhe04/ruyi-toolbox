"""设备选择：可插拔的一层，一档坏了自动降到下一档。

为什么单独一个模块：显卡后端是本 shim 最容易坏、也最无法在一台机器上测全的地方
（开发时只有英伟达卡；AMD 那档后来在 RX 7650 GRE 上真机验证过，其余几档仍没有）。把「有哪些档、按什么顺序试、坏了怎么降」抽出来，
就能用假探针把**选择与降级逻辑**整个测掉，不需要真的有那块卡。

auto 的顺序：

    cuda/rocm  →  xpu  →  mps  →  directml  →  cpu

**AMD 显卡走的就是第一档。** 这是查证结论（docs/backend-notes.md §8），不是猜：AMD 官方的
Windows 版 ROCm PyTorch 轮子里，`torch.cuda.is_available()` 返回 True、设备字符串仍然是 `"cuda"`、
`torch.version.hip` 非 None。所以**不需要为 AMD 单开分支**，只需要在日志与 `/health` 里如实
区分 rocm 与 cuda，别让用户以为自己在用英伟达。

任何一档初始化失败都降到下一档，并留一句人话。**绝不因为显卡后端坏了就整个服务起不来。**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

LOG = logging.getLogger("ruyi_asr_shim.devices")

# 用户能填的值 → 内部档位名
PREF_ALIASES = {
    "": "auto",
    "auto": "auto",
    "cuda": "cuda",
    "gpu": "cuda",
    "nvidia": "cuda",
    # ROCm 版 torch 复用 torch.cuda 接口，AMD 与英伟达是同一档，不是两条路
    "rocm": "cuda",
    "hip": "cuda",
    "amd": "cuda",
    "xpu": "xpu",
    "intel": "xpu",
    "mps": "mps",
    "directml": "directml",
    "dml": "directml",
    "cpu": "cpu",
}

# auto 的尝试顺序
AUTO_ORDER = ("cuda", "xpu", "mps", "directml", "cpu")

DTYPE_NAMES = ("auto", "bfloat16", "float16", "float32")


@dataclass(frozen=True)
class DeviceChoice:
    kind: str        # cuda / rocm / xpu / mps / directml / cpu
    device: object   # 传给 model.to() 的东西（字符串或 torch 设备对象）
    dtype: object    # torch dtype
    label: str       # 给人看的一行，也就是 /health 里的 device 字段

    def __str__(self) -> str:
        return self.label


class DeviceUnavailable(Exception):
    """这一档用不了。多数是正常情况（没这块卡、没装那个包），不是错误。"""


Probe = Callable[[], DeviceChoice]


# ── dtype ──────────────────────────────────────────────────────────────────


def resolve_dtype(torch, requested: str, default_name: str):
    """把 `RUYI_ASR_DTYPE` 解成真的 torch dtype。填错就按 default 来，不让服务因此起不来。"""
    name = str(requested or "auto").strip().lower()
    if name not in DTYPE_NAMES:
        LOG.warning("看不懂的 RUYI_ASR_DTYPE=%r，按 auto 处理（可选：%s）",
                    requested, "/".join(DTYPE_NAMES))
        name = "auto"
    if name == "auto":
        name = default_name
    dtype = getattr(torch, name, None)
    if dtype is None:  # 极老的 torch 没有 bfloat16
        LOG.warning("这个 torch 没有 %s，退到 float32", name)
        return torch.float32, "float32"
    return dtype, name


# ── 各档探针 ────────────────────────────────────────────────────────────────


def probe_cuda(torch, dtype_pref: str = "auto") -> DeviceChoice:
    """英伟达 CUDA 与 AMD ROCm／HIP 是同一档：两者都用 torch.cuda 接口、设备字符串都是 "cuda"。"""
    if not hasattr(torch, "cuda") or not torch.cuda.is_available():
        raise DeviceUnavailable("torch.cuda 报告没有可用设备")
    try:
        name = torch.cuda.get_device_name(0)
    except Exception as exc:
        raise DeviceUnavailable("拿不到设备名：%s" % (str(exc)[:120])) from exc

    hip = getattr(getattr(torch, "version", None), "hip", None)
    is_rocm = hip is not None
    kind = "rocm" if is_rocm else "cuda"

    # dtype 缺省值分两种：
    #   英伟达 —— bfloat16（卡支持就用，Ampere 及以后都支持）。
    #   AMD    —— float16。AMD 的 Windows 支持矩阵只承诺 FP16（RDNA4 另加 FP8），**通篇没提 BF16**，
    #             而且 gfx1100 上有过 bf16 相关的 MIOpen 崩溃记录。别假设 bfloat16 处处可用。
    if is_rocm:
        default_name = "float16"
    else:
        default_name = "float32"
        try:
            default_name = "bfloat16" if torch.cuda.is_bf16_supported() else "float16"
        except Exception:
            default_name = "float16"
    dtype, dtype_name = resolve_dtype(torch, dtype_pref, default_name)

    # 真的碰一下显存：驱动与轮子对不上时这里就会炸（AMD 那边 hipErrorInvalidImage 就是这个形状），
    # 早炸早降级，好过加载到一半才崩。
    # synchronize 不能省：内核是异步发的，不等它，内核层面的错误到不了这个 try 里；而且 Windows 上的 ROCm
    # 有个实测的坑——发了内核没同步就退出的进程会卡在退出阶段不走（doctor 因此挂住，install.ps1 的自检跟着挂）。
    try:
        probe = torch.zeros(8, dtype=dtype, device="cuda")
        torch.cuda.synchronize()
        del probe
    except Exception as exc:
        raise DeviceUnavailable(
            "%s 设备在，但分配不了显存（多半是 torch 构建与这块卡对不上）：%s"
            % (kind, str(exc)[:200])
        ) from exc

    suffix = ("，ROCm/HIP " + str(hip)) if is_rocm else ""
    return DeviceChoice(kind=kind, device="cuda", dtype=dtype,
                        label="%s (%s, %s%s)" % (kind, name, dtype_name, suffix))


def probe_xpu(torch, dtype_pref: str = "auto") -> DeviceChoice:
    """Intel Arc / Intel GPU。torch 从 2.5 起内置 xpu 后端。"""
    xpu = getattr(torch, "xpu", None)
    if xpu is None or not xpu.is_available():
        raise DeviceUnavailable("torch.xpu 不可用")
    try:
        name = xpu.get_device_name(0)
    except Exception:
        name = "Intel GPU"
    dtype, dtype_name = resolve_dtype(torch, dtype_pref, "float16")
    try:
        probe = torch.zeros(8, dtype=dtype, device="xpu")
        del probe
    except Exception as exc:
        raise DeviceUnavailable("xpu 设备在，但分配不了显存：%s" % (str(exc)[:200])) from exc
    return DeviceChoice(kind="xpu", device="xpu", dtype=dtype,
                        label="xpu (%s, %s)" % (name, dtype_name))


def probe_mps(torch, dtype_pref: str = "auto") -> DeviceChoice:
    """Apple Silicon。本 shim 主要面向 Windows，但这一档不要钱，顺手留着。"""
    backends = getattr(torch, "backends", None)
    mps = getattr(backends, "mps", None) if backends is not None else None
    if mps is None or not mps.is_available():
        raise DeviceUnavailable("torch.backends.mps 不可用")
    dtype, dtype_name = resolve_dtype(torch, dtype_pref, "float16")
    try:
        probe = torch.zeros(8, dtype=dtype, device="mps")
        del probe
    except Exception as exc:
        raise DeviceUnavailable("mps 设备在，但分配不了显存：%s" % (str(exc)[:200])) from exc
    return DeviceChoice(kind="mps", device="mps", dtype=dtype,
                        label="mps (Apple Silicon, %s)" % dtype_name)


def probe_directml(torch, dtype_pref: str = "auto") -> DeviceChoice:
    """微软 torch-directml（任何 DX12 显卡）。

    **这一档今天基本是死路，我们的安装脚本不装它**，留着只是为了「万一你自己装了而且能用」：
      - torch-directml 最新版 0.2.5.dev240914（2024-09-15，两年没动）把 torch 钉死在 `torch==2.4.1`，
        而 transformers 5.x 要 `torch>=2.5` —— 直接冲突，pip 解不开。
      - microsoft/DirectML 自己在 README 顶上写着 "DirectML is in maintenance mode"。
      - bfloat16 明确不支持（DirectML issue #688，closed as not planned）。
    详见 docs/backend-notes.md §8。AMD 用户请走 ROCm on Windows（第一档）。
    """
    try:
        import torch_directml  # type: ignore  # noqa: PLC0415
    except ImportError as exc:
        raise DeviceUnavailable("没装 torch-directml") from exc
    except Exception as exc:  # 装了但和当前 torch 对不上
        raise DeviceUnavailable("torch-directml 装了但加载失败：%s" % (str(exc)[:200])) from exc

    try:
        if hasattr(torch_directml, "is_available") and not torch_directml.is_available():
            raise DeviceUnavailable("torch-directml 报告没有可用设备")
        dml = torch_directml.device()
        try:
            name = torch_directml.device_name(0)
        except Exception:
            name = "DirectML"
    except DeviceUnavailable:
        raise
    except Exception as exc:
        raise DeviceUnavailable("torch-directml 初始化失败：%s" % (str(exc)[:200])) from exc

    # DirectML 不支持 bfloat16（上面那条 issue），float16 覆盖也不全 —— 缺省 float32 换「能跑」。
    dtype, dtype_name = resolve_dtype(torch, dtype_pref, "float32")
    try:
        probe = torch.zeros(8, dtype=dtype, device=dml)
        del probe
    except Exception as exc:
        raise DeviceUnavailable("DirectML 设备在，但分配不了显存：%s" % (str(exc)[:200])) from exc

    return DeviceChoice(kind="directml", device=dml, dtype=dtype,
                        label="directml (%s, %s)" % (name, dtype_name))


def probe_cpu(torch, dtype_pref: str = "auto") -> DeviceChoice:
    """最后一档，永不失败。CPU 上一律 float32：半精度在 CPU 上通常更慢。"""
    dtype, dtype_name = resolve_dtype(torch, dtype_pref, "float32")
    return DeviceChoice(kind="cpu", device="cpu", dtype=dtype,
                        label="cpu (%s)" % dtype_name)


def build_probes(torch, dtype_pref: str = "auto") -> list[tuple[str, Probe]]:
    return [
        ("cuda", lambda: probe_cuda(torch, dtype_pref)),
        ("xpu", lambda: probe_xpu(torch, dtype_pref)),
        ("mps", lambda: probe_mps(torch, dtype_pref)),
        ("directml", lambda: probe_directml(torch, dtype_pref)),
        ("cpu", lambda: probe_cpu(torch, dtype_pref)),
    ]


# ── 选择与降级 ──────────────────────────────────────────────────────────────


def normalize_preference(pref: str) -> str:
    key = str(pref or "").strip().lower()
    if key not in PREF_ALIASES:
        LOG.warning("看不懂的 RUYI_ASR_DEVICE=%r，按 auto 处理"
                    "（可选：auto/cuda/amd/rocm/xpu/mps/directml/cpu）", pref)
        return "auto"
    return PREF_ALIASES[key]


def order_for(preference: str) -> list[str]:
    """要试的档位顺序。显式指定的那一档排最前，后面仍接上 auto 的顺序当降级路径。"""
    pref = normalize_preference(preference)
    if pref == "auto":
        return list(AUTO_ORDER)
    if pref == "cpu":
        return ["cpu"]  # 明说要 CPU 就老老实实 CPU，不去碰显卡
    return [pref] + [k for k in AUTO_ORDER if k != pref]


def select_device(preference: str, probes: list[tuple[str, Probe]]) -> DeviceChoice:
    """按顺序试，第一个成功的就是它。"""
    wanted = order_for(preference)
    available = dict(probes)
    tried: list[str] = []
    for kind in wanted:
        probe = available.get(kind)
        if probe is None:
            continue
        tried.append(kind)
        try:
            choice = probe()
        except DeviceUnavailable as exc:
            # 「没装 torch-directml」「机器上没有 Intel GPU」这种是常态，不值得吓人。
            LOG.info("%s 这一档用不了（%s），试下一档", kind, exc)
            continue
        except Exception as exc:
            LOG.warning("%s 这一档初始化失败（%s），降到下一档", kind, str(exc)[:200])
            continue
        if kind != wanted[0]:
            LOG.warning("没能用上 %s，实际用的是 %s —— 见上面几行的原因", wanted[0], choice.kind)
        if choice.kind == "cpu" and wanted[0] != "cpu":
            LOG.warning("没有可用的显卡加速，现在是 CPU 推理，会很慢"
                        "（一段十几秒的音频可能要几十秒到几分钟）。"
                        "装显卡加速的办法见 asr-shim/README.md。")
        return choice
    raise RuntimeError("所有设备档位都用不了（试过：%s）" % ", ".join(tried))
