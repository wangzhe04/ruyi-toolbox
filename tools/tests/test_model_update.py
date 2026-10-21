import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("model_update", ROOT / "tools/model_update.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
sys.path.insert(0, str(ROOT / "asr-shim"))


class ModelUpdateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.install = self.base / "原安装"
        self.install.mkdir()
        shutil.copytree(ROOT / "asr-shim/ruyi_asr_shim", self.install / "ruyi_asr_shim", ignore=shutil.ignore_patterns("__pycache__"))
        self.models = self.base / "独立模型目录"
        old = self.models / "Qwen3-ASR-0.6B-hf"
        old.mkdir(parents=True)
        (old / "config.json").write_text("{}")
        (old / "model.safetensors").write_bytes(b"old")
        self.record_path = self.base / "登记/asr-shim.json"
        self.record_path.parent.mkdir()
        self.record = {
            "id": "asr-shim", "schema": 1,
            "run": {"command": sys.executable, "cwd": str(self.install),
                    "args": ["-m", "ruyi_asr_shim"], "env": {"RUYI_ASR_MODELS_ROOT": str(self.models), "RUYI_ASR_MODEL": "auto", "CUSTOM": "keep"}},
            "service": {"port": 9876},
            "provides": [{"type": "asr", "model": "qwen3-asr-auto", "basePath": "/v1"}],
        }
        self.record_path.write_text(json.dumps(self.record), encoding="utf-8")
        self.bundle = self.base / "补充包"
        self.model_name = "Qwen3-ASR-1.7B-hf"
        source = self.bundle / "asr-shim/models" / self.model_name
        source.mkdir(parents=True)
        (source / "config.json").write_text("{}")
        (source / "model.safetensors").write_bytes(b"new")
        self.source = source
        self.manifest = {"mode": "model-update", "components": [{"id": "asr-shim", "modelFiles": {
            self.model_name: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
        }}]}
        (self.bundle / "bundle-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_update_and_repeat_preserve_old_install_and_settings(self):
        update.apply(self.bundle, self.record_path)
        update.apply(self.bundle, self.record_path)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["run"], self.record["run"])
        self.assertEqual(record["service"], {"port": 9876})
        self.assertEqual([m["id"] for m in record["provides"][0]["models"]],
                         ["qwen3-asr-auto", "qwen3-asr-0.6b", "qwen3-asr-1.7b"])
        self.assertEqual((self.models / "Qwen3-ASR-0.6B-hf/model.safetensors").read_bytes(), b"old")

    def test_corrupt_bundle_does_not_change_registration(self):
        before = self.record_path.read_bytes()
        (self.source / "model.safetensors").write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            update.apply(self.bundle, self.record_path)
        self.assertEqual(self.record_path.read_bytes(), before)
        self.assertFalse((self.models / self.model_name).exists())

    def test_old_single_model_registration_is_upgraded(self):
        env = self.record["run"]["env"]
        env.pop("RUYI_ASR_MODELS_ROOT")
        env["RUYI_ASR_MODEL_DIR"] = str(self.models / "Qwen3-ASR-0.6B-hf")
        env["RUYI_ASR_MODEL"] = "qwen3-asr-0.6b"
        self.record_path.write_text(json.dumps(self.record), encoding="utf-8")
        update.apply(self.bundle, self.record_path)
        saved = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["run"]["env"]["RUYI_ASR_MODEL"], "auto")
        self.assertNotIn("RUYI_ASR_MODEL_DIR", saved["run"]["env"])
        self.assertEqual(len(saved["provides"][0]["models"]), 3)

    def test_missing_old_models_does_not_create_new_install(self):
        shutil.rmtree(self.models)
        with self.assertRaisesRegex(ValueError, "原安装自检失败"):
            update.apply(self.bundle, self.record_path)
        self.assertFalse(self.models.exists())

    def test_failed_copy_does_not_leave_partial_model(self):
        with patch.object(update.shutil, "copytree", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                update.apply(self.bundle, self.record_path)
        self.assertFalse((self.models / self.model_name).exists())
        self.assertFalse(list(self.models.glob(".ruyi-model-*")))

    def test_conflicting_existing_model_is_not_overwritten(self):
        dest = self.models / self.model_name
        shutil.copytree(self.source, dest)
        (dest / "model.safetensors").write_bytes(b"different")
        with self.assertRaises(ValueError):
            update.apply(self.bundle, self.record_path)
        self.assertEqual((dest / "model.safetensors").read_bytes(), b"different")

    def test_registration_failure_rolls_back_new_model(self):
        before = self.record_path.read_bytes()
        with patch("ruyi_asr_shim.registry.write_record", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                update.apply(self.bundle, self.record_path)
        self.assertFalse((self.models / self.model_name).exists())
        self.assertEqual(self.record_path.read_bytes(), before)

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell required")
    def test_pack_extract_and_run_with_registered_python(self):
        repo = self.base / "repo"
        component = repo / "asr-shim"
        shutil.copytree(self.bundle / "asr-shim/models", component / "models")
        (component / "scripts").mkdir()
        (component / "scripts/install.ps1").write_text("throw 'must not install'")
        (component / "ruyi_asr_shim").mkdir()
        (component / "ruyi_asr_shim/__main__.py").write_text("")
        (component / "pyproject.toml").write_text('[project]\ndependencies = []\n')
        out = self.base / "out"
        result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(ROOT / "tools/package-bundle.ps1"), "-Root", str(repo), "-Components", "asr-shim",
            "-Models", "asr-shim=qwen3-asr-1.7b", "-ModelUpdate", "-OutputDir", str(out), "-ZipName", "update"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        extracted = self.base / "解压"
        with zipfile.ZipFile(out / "update.zip") as archive:
            self.assertNotIn("asr-shim/scripts/install.ps1", archive.namelist())
            archive.extractall(extracted)
        env = dict(os.environ, RUYI_TOOLBOX_COMPONENTS_DIR=str(self.record_path.parent))
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(extracted / "setup.ps1")]
        for _ in range(2):
            result = subprocess.run(command, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.models / self.model_name / "model.safetensors").exists())
        self.record_path.unlink()
        result = subprocess.run(command, env=env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((extracted / "asr-shim/.venv").exists())


if __name__ == "__main__":
    unittest.main()
