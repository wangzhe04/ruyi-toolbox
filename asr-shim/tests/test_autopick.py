"""模型自动挑选与清单（用户 2026-09-21 下午拍板：auto 缺省 0.6B、其余尺寸在如意里自己选）：纯函数，不碰 torch。"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ruyi_asr_shim.autopick import AUTO_MODEL_NAME, CANDIDATES, by_name, catalog, free_vram_mb, installed, is_auto, looks_installed, normalize_prefer, pick
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

    def test_pick_default_is_smallest(self):
        """用户 2026-09-21 下午改口:auto 缺省 0.6B(省显存),1.7B 让用户在如意里自己选。"""
        make_model(self.tmp, "Qwen3-ASR-0.6B-hf")
        make_model(self.tmp, "Qwen3-ASR-1.7B-hf")
        for free in (8000, 3000, None):
            c, why = pick(self.tmp, free)
            self.assertEqual(c.name, "qwen3-asr-0.6b", "free=%r" % free)
            self.assertIn("省显存", why)
        c, why = pick(self.tmp, 1000)
        self.assertEqual(c.name, "qwen3-asr-0.6b")
        self.assertIn("少", why)

    def test_pick_prefer_large_keeps_old_policy(self):
        make_model(self.tmp, "Qwen3-ASR-0.6B-hf")
        make_model(self.tmp, "Qwen3-ASR-1.7B-hf")
        c, why = pick(self.tmp, 8000, prefer="large")
        self.assertEqual(c.name, "qwen3-asr-1.7b")
        self.assertIn("最大能装下", why)
        c, _ = pick(self.tmp, 3000, prefer="large")
        self.assertEqual(c.name, "qwen3-asr-0.6b")
        c, why = pick(self.tmp, 1000, prefer="large")
        self.assertEqual(c.name, "qwen3-asr-0.6b")
        self.assertIn("不够", why)
        c, why = pick(self.tmp, None, prefer="large")
        self.assertEqual(c.name, "qwen3-asr-0.6b")
        self.assertIn("没有显卡", why)
        self.assertEqual(normalize_prefer("LARGE"), "large")
        self.assertEqual(normalize_prefer("whatever"), "small")

    def test_catalog_and_by_name(self):
        """清单:auto 在前,装好的尺寸按小到大;每项带给设置页看的 label。"""
        self.assertEqual([m["id"] for m in catalog(self.tmp)], [AUTO_MODEL_NAME], "一份都没装也列 auto")
        make_model(self.tmp, "Qwen3-ASR-1.7B-hf")
        self.assertEqual([m["id"] for m in catalog(self.tmp)], [AUTO_MODEL_NAME, "qwen3-asr-1.7b"])
        make_model(self.tmp, "Qwen3-ASR-0.6B-hf")
        cat = catalog(self.tmp)
        self.assertEqual([m["id"] for m in cat], [AUTO_MODEL_NAME, "qwen3-asr-0.6b", "qwen3-asr-1.7b"])
        self.assertTrue(all(m["label"] for m in cat))
        self.assertIn("显存", cat[2]["label"])
        self.assertEqual(by_name("QWEN3-ASR-1.7B").dirname, "Qwen3-ASR-1.7B-hf")
        self.assertIsNone(by_name("whisper-1"))
        self.assertIsNone(by_name(AUTO_MODEL_NAME), "auto 不是一份具体的尺寸")

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
        self.assertEqual(rec["service"]["unload"], "/v1/unload", "133:切走即卸载的那条路要写进登记")
        self.assertEqual([m["id"] for m in rec["provides"][0]["models"]], [AUTO_MODEL_NAME], "C:\\m 里一份都没装 → 清单只有 auto")
        rec2 = build_record(python_exe="C:\\py.exe", cwd="C:\\x", model_dir="C:\\m\\a", model_name="qwen3-asr-1.7b", port=8790)
        self.assertEqual(rec2["run"]["env"], {"RUYI_ASR_MODEL_DIR": "C:\\m\\a", "RUYI_ASR_MODEL": "qwen3-asr-1.7b"})
        self.assertEqual(rec2["provides"][0]["model"], "qwen3-asr-1.7b")
        self.assertNotIn("models", rec2["provides"][0], "单模型模式不列清单")

    def test_record_lists_installed_sizes(self):
        make_model(self.root, "Qwen3-ASR-1.7B-hf")
        rec = build_record(python_exe="C:\\py.exe", cwd="C:\\x", model_dir="", model_name="auto", port=8790, models_root=self.root)
        self.assertEqual([m["id"] for m in rec["provides"][0]["models"]], [AUTO_MODEL_NAME, "qwen3-asr-0.6b", "qwen3-asr-1.7b"])
        self.assertTrue(all(isinstance(m["label"], str) and m["label"] for m in rec["provides"][0]["models"]))

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
