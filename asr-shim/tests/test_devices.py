"""设备选择与降级（不只针对英伟达）。

开发机只有英伟达卡时，AMD／Intel／Apple 那几档**没法真机验证**（AMD 后来在 RX 7650 GRE 上真机跑通过，见 README）。所以这里用假探针把
【选择与降级逻辑】整个测掉：顺序对不对、一档坏了会不会降、显式指定了但用不了会不会降、
CPU 是不是永远兜得住。真机验过的是 cuda、rocm 与 cpu 三档（见 README「实测」与「AMD 实测」）。
"""

from __future__ import annotations

import logging
import unittest

from ruyi_asr_shim.devices import (
    AUTO_ORDER,
    DeviceChoice,
    DeviceUnavailable,
    normalize_preference,
    order_for,
    probe_cpu,
    probe_cuda,
    probe_directml,
    resolve_dtype,
    select_device,
)


def choice(kind):
    return DeviceChoice(kind=kind, device=kind, dtype="dt-" + kind, label=kind + " (fake)")


def ok(kind):
    return lambda: choice(kind)


def unavailable(kind, why="没有这块卡"):
    def probe():
        raise DeviceUnavailable(why)
    return probe


def broken(kind, why="驱动炸了"):
    def probe():
        raise RuntimeError(why)
    return probe


def probes(**kinds):
    """按 AUTO_ORDER 的顺序组装探针表。"""
    return [(k, kinds[k]) for k in AUTO_ORDER if k in kinds]


class FakeTorch:
    """只长了 devices.py 会碰的那几个属性。"""

    class version:
        cuda = "12.8"
        hip = None

    class _Cuda:
        def __init__(self, available=True, bf16=True, name="Fake GPU", alloc_ok=True, sync_ok=True):
            self._available = available
            self._bf16 = bf16
            self._name = name
            self.alloc_ok = alloc_ok
            self.sync_ok = sync_ok
            self.synced = 0

        def is_available(self):
            return self._available

        def is_bf16_supported(self):
            return self._bf16

        def get_device_name(self, i):
            return self._name

        def synchronize(self):
            self.synced += 1
            if not self.sync_ok:
                raise RuntimeError("hipErrorInvalidDeviceFunction")

    bfloat16 = "bfloat16"
    float16 = "float16"
    float32 = "float32"

    def __init__(self, **kw):
        self.cuda = FakeTorch._Cuda(**kw)
        self._alloc_ok = self.cuda.alloc_ok

    def zeros(self, n, dtype=None, device=None):
        if not self._alloc_ok:
            raise RuntimeError("hipErrorInvalidImage")
        return object()


class TestPreference(unittest.TestCase):
    def test_aliases(self):
        for word in ("amd", "rocm", "hip", "nvidia", "gpu", "cuda"):
            self.assertEqual(normalize_preference(word), "cuda",
                             "%s 应当落在 cuda 那一档（ROCm 复用 torch.cuda）" % word)
        self.assertEqual(normalize_preference("dml"), "directml")
        self.assertEqual(normalize_preference("intel"), "xpu")
        self.assertEqual(normalize_preference(""), "auto")
        self.assertEqual(normalize_preference(None), "auto")

    def test_unknown_preference_falls_back_to_auto(self):
        self.assertEqual(normalize_preference("显卡"), "auto")

    def test_auto_order_starts_with_cuda_ends_with_cpu(self):
        self.assertEqual(order_for("auto")[0], "cuda")
        self.assertEqual(order_for("auto")[-1], "cpu")

    def test_explicit_choice_goes_first_but_keeps_fallbacks(self):
        o = order_for("directml")
        self.assertEqual(o[0], "directml")
        self.assertIn("cpu", o, "显式指定了也要留降级路径，不能一坏就整个起不来")
        self.assertEqual(len(set(o)), len(o))

    def test_explicit_cpu_does_not_touch_gpu(self):
        self.assertEqual(order_for("cpu"), ["cpu"])


class TestSelect(unittest.TestCase):
    def test_auto_prefers_cuda(self):
        c = select_device("auto", probes(cuda=ok("cuda"), directml=ok("directml"), cpu=ok("cpu")))
        self.assertEqual(c.kind, "cuda")

    def test_auto_falls_through_to_cpu(self):
        c = select_device("auto", probes(
            cuda=unavailable("cuda"), xpu=unavailable("xpu"),
            directml=unavailable("directml"), cpu=ok("cpu"),
        ))
        self.assertEqual(c.kind, "cpu")

    def test_broken_tier_degrades_instead_of_crashing(self):
        """显卡后端炸了要降级，不是让整个服务起不来。"""
        c = select_device("auto", probes(
            cuda=broken("cuda", "CUDA driver version is insufficient"),
            directml=broken("directml", "版本对不上"),
            cpu=ok("cpu"),
        ))
        self.assertEqual(c.kind, "cpu")

    def test_explicit_preference_degrades_too(self):
        c = select_device("directml", probes(
            cuda=ok("cuda"), directml=broken("directml"), cpu=ok("cpu"),
        ))
        self.assertEqual(c.kind, "cuda", "directml 用不了要接着往下试，不是直接死")

    def test_explicit_cpu_is_respected_even_with_gpu_present(self):
        c = select_device("cpu", probes(cuda=ok("cuda"), cpu=ok("cpu")))
        self.assertEqual(c.kind, "cpu")

    def test_amd_preference_maps_to_cuda_tier(self):
        c = select_device("amd", probes(cuda=ok("rocm"), cpu=ok("cpu")))
        self.assertEqual(c.kind, "rocm")

    def test_degradation_is_logged_in_human_words(self):
        with self.assertLogs("ruyi_asr_shim.devices", level=logging.INFO) as cap:
            select_device("auto", probes(cuda=broken("cuda", "驱动炸了"), cpu=ok("cpu")))
        blob = "\n".join(cap.output)
        self.assertIn("驱动炸了", blob)
        self.assertIn("CPU 推理", blob, "降到 CPU 要明说会很慢")

    def test_everything_broken_raises(self):
        with self.assertRaises(RuntimeError):
            select_device("auto", probes(cuda=broken("cuda"), cpu=broken("cpu")))


class TestCudaProbe(unittest.TestCase):
    def test_nvidia_uses_bfloat16_when_supported(self):
        c = probe_cuda(FakeTorch(bf16=True, name="NVIDIA GeForce RTX 5080"))
        self.assertEqual(c.kind, "cuda")
        self.assertEqual(c.device, "cuda")
        self.assertEqual(c.dtype, "bfloat16")
        self.assertIn("RTX 5080", c.label)

    def test_nvidia_without_bf16_uses_float16(self):
        c = probe_cuda(FakeTorch(bf16=False))
        self.assertEqual(c.dtype, "float16")

    def test_rocm_is_reported_as_rocm_and_defaults_to_float16(self):
        """AMD 的 ROCm 构建：torch.cuda.is_available() 为真、设备字符串仍是 cuda，
        但 /health 与日志要如实说是 rocm；dtype 缺省 float16（AMD 的 Windows 文档只承诺 FP16）。"""
        t = FakeTorch(bf16=True, name="AMD Radeon RX 7900 XTX")
        t.version.hip = "7.2.53211"
        try:
            c = probe_cuda(t)
            self.assertEqual(c.kind, "rocm")
            self.assertEqual(c.device, "cuda", "ROCm 上设备字符串还是 cuda，不需要分叉")
            self.assertEqual(c.dtype, "float16", "别假设 bfloat16 在 AMD 上能用")
            self.assertIn("ROCm", c.label)
            self.assertIn("RX 7900", c.label)
        finally:
            t.version.hip = None

    def test_no_device_raises_unavailable(self):
        with self.assertRaises(DeviceUnavailable):
            probe_cuda(FakeTorch(available=False))

    def test_allocation_failure_raises_unavailable(self):
        """驱动/轮子对不上时（AMD 那边的 hipErrorInvalidImage 就是这形状）要判成这一档用不了。"""
        with self.assertRaises(DeviceUnavailable):
            probe_cuda(FakeTorch(alloc_ok=False))

    def test_async_kernel_failure_surfaces_at_synchronize(self):
        """内核是异步发的：分配那一下没报错、错误要到 synchronize 才冒出来，也得判成用不了。"""
        with self.assertRaises(DeviceUnavailable):
            probe_cuda(FakeTorch(sync_ok=False))

    def test_probe_synchronizes(self):
        """Windows ROCm 上发了内核不同步就退出，进程会卡在退出阶段（doctor 因此挂住过）。"""
        t = FakeTorch()
        probe_cuda(t)
        self.assertGreaterEqual(t.cuda.synced, 1)

    def test_dtype_override(self):
        c = probe_cuda(FakeTorch(bf16=True), dtype_pref="float32")
        self.assertEqual(c.dtype, "float32")


class TestOtherProbes(unittest.TestCase):
    def test_cpu_never_fails(self):
        c = probe_cpu(FakeTorch(available=False))
        self.assertEqual(c.kind, "cpu")
        self.assertEqual(c.dtype, "float32")

    def test_directml_absent_is_unavailable_not_crash(self):
        # 这台机器上没装 torch-directml（我们的安装脚本也不装它），应当是「用不了」而不是崩。
        with self.assertRaises(DeviceUnavailable):
            probe_directml(FakeTorch())


class TestDtype(unittest.TestCase):
    def test_auto_uses_default(self):
        self.assertEqual(resolve_dtype(FakeTorch(), "auto", "bfloat16"), ("bfloat16", "bfloat16"))

    def test_explicit_wins(self):
        self.assertEqual(resolve_dtype(FakeTorch(), "float16", "bfloat16"), ("float16", "float16"))

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(resolve_dtype(FakeTorch(), "int4", "float32"), ("float32", "float32"))


if __name__ == "__main__":
    unittest.main()
