"""模型自动挑选（用户 2026-09-21 拍板「有多份就用更大的，显存允许的话」）：纯函数，不碰 torch。"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ruyi_asr_shim.autopick import AUTO_MODEL_NAME, CANDIDATES, free_vram_mb, installed, is_auto, looks_installed, pick
from ruyi_asr_shim.registry import DIR_ENV, build_record, register, registration_path, self_check


def make_model(root: str, dirname: str, complete: bool = True) -> str:
    d = os.path.join(root, dirname)
    os.makedirs(d, exist_ok=True)
    Path(d, "config.json").write_text("{}", encoding="utf-8")
    Path(d, "model.safetensors" if complete else "model.safetensors.incomplete").write_bytes(b"x")
    return d


class FakeCuda:
    def __init__(self, free_mb):
        self.free_mb = free_mb

    def is_available(self):
        return self.free_mb is not None

    def mem_get_info(self):
        return self.free_mb * 1024 * 1024, 8 * 1024 * 1024 * 1024


class FakeTorch:
    def __init__(self, free_mb):
        self.cuda = FakeCuda(free_mb)


class AutopickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asr-autopick-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_names(self):
        self.assertTrue(is_auto("auto") and is_auto("AUTO") and is_auto(AUTO_MODEL_NAME))
        self.assertFalse(is_auto("qwen3-asr-0.6b") or is_auto(""))
        self.assertEqual([c.name for c in CANDIDATES], ["qwen3-asr-1.7b", "qwen3-asr-0.6b"], "大在前")

    def test_installed_ignores_incomplete(self):
        make_model(self.tmp, "Qwen3-ASR-0.6B-hf")
        make_model(self.tmp, "Qwen3-ASR-1.7B-hf", complete=False)
        self.assertEqual([c.name for c in installed(self.tmp)], ["qwen3-asr-0.6b"])
        self.assertFalse(looks_installed(os.path.join(self.tmp, "nope")))
        self.assertEqual(installed(os.path.join(self.tmp, "nope")), [])

    def test_pick_largest_that_fits(self):
        make_model(self.tmp, "Qwen3-ASR-0.6B-hf")
        make_model(self.tmp, "Qwen3-ASR-1.7B-hf")
        c, why = pick(self.tmp, 8000)
        self.assertEqual(c.name, "qwen3-asr-1.7b")
        self.assertIn("最大能装下", why)
        c, _ = pick(self.tmp, 3000)
        self.assertEqual(c.name, "qwen3-asr-0.6b")
        c, why = pick(self.tmp, 1000)
        self.assertEqual(c.name, "qwen3-asr-0.6b")
        self.assertIn("不够", why)
        c, why = pick(self.tmp, None)
        self.assertEqual(c.name, "qwen3-asr-0.6b")
        self.assertIn("没有显卡", why)

    def test_pick_only_one_installed_or_none(self):
        make_model(self.tmp, "Qwen3-ASR-1.7B-hf")
        c, _ = pick(self.tmp, 1000)
        self.assertEqual(c.name, "qwen3-asr-1.7b", "只有一份就是它，装不装得下由加载那层退 CPU 兜")
        c, why = pick(os.path.join(self.tmp, "empty"), 8000)
        self.assertIsNone(c)
        self.assertIn("没有下全", why)

    def test_free_vram_mb_never_raises(self):
        self.assertEqual(free_vram_mb(FakeTorch(5000)), 5000)
        self.assertIsNone(free_vram_mb(FakeTorch(None)))
        self.assertIsNone(free_vram_mb(object()))


class AutoRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asr-autoreg-")
        self.env = {DIR_ENV: os.path.join(self.tmp, "components")}
        self.pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.root = os.path.join(self.tmp, "models")
        make_model(self.root, "Qwen3-ASR-0.6B-hf")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_self_check_auto(self):
        self.assertTrue(self_check(sys.executable, self.pkg_root, "", self.root).ok)
        self.assertFalse(self_check(sys.executable, self.pkg_root, "", os.path.join(self.tmp, "nope")).ok)
        self.assertFalse(self_check(sys.executable, self.pkg_root, "", "models").ok)
        shutil.rmtree(os.path.join(self.root, "Qwen3-ASR-0.6B-hf"))
        self.assertFalse(self_check(sys.executable, self.pkg_root, "", self.root).ok)

    def test_record_shape_auto(self):
        rec = build_record(python_exe="C:\\py.exe", cwd="C:\\x", model_dir="", model_name="auto", port=8790, models_root="C:\\m")
        self.assertEqual(rec["run"]["env"], {"RUYI_ASR_MODEL": "auto", "RUYI_ASR_MODELS_ROOT": "C:\\m"})
        self.assertEqual(rec["provides"][0]["model"], AUTO_MODEL_NAME)
        rec2 = build_record(python_exe="C:\\py.exe", cwd="C:\\x", model_dir="C:\\m\\a", model_name="qwen3-asr-1.7b", port=8790)
        self.assertEqual(rec2["run"]["env"], {"RUYI_ASR_MODEL_DIR": "C:\\m\\a", "RUYI_ASR_MODEL": "qwen3-asr-1.7b"})
        self.assertEqual(rec2["provides"][0]["model"], "qwen3-asr-1.7b")

    def test_register_auto(self):
        out = io.StringIO()
        self.assertEqual(register(model_dir="", model_name="auto", port=8790, env=self.env, out=out, models_root=self.root), 0)
        rec = json.loads(registration_path(self.env).read_text("utf-8"))
        self.assertEqual(rec["run"]["env"]["RUYI_ASR_MODELS_ROOT"], self.root)
        self.assertEqual(rec["provides"][0]["model"], AUTO_MODEL_NAME)
        out = io.StringIO()
        self.assertEqual(register(model_dir="", model_name="auto", port=8790, env=self.env, out=out), 3)
        self.assertIn("--models-root", out.getvalue())


if __name__ == "__main__":
    unittest.main()
