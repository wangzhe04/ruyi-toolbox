"""组件登记（docs/00-component-registry.md）的判据。

纪律：测试绝不写真的 `~/.ruyi-toolbox` —— 全部用 RUYI_TOOLBOX_COMPONENTS_DIR 改道到临时目录。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ruyi_asr_shim import COMPONENT_ID, COMPONENT_NAME_TAG, __version__
from ruyi_asr_shim import registry as registry_mod
from ruyi_asr_shim.registry import (
    DIR_ENV,
    build_record,
    model_dir_problems,
    register,
    registration_path,
    self_check,
    unregister,
    write_record,
)

from .helpers import fake_python, fake_python_args

PKG_ROOT = Path(__file__).resolve().parent.parent  # asr-shim/


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ruyi-reg-test-"))
        self.components = self.tmp / "components"
        self.env = {DIR_ENV: str(self.components)}
        # 一份「看起来下全了」的假模型目录
        self.model_dir = self.tmp / "models" / "Qwen3-ASR-0.6B-hf"
        self.model_dir.mkdir(parents=True)
        (self.model_dir / "config.json").write_text('{"model_type": "qwen3_asr"}', encoding="utf-8")
        (self.model_dir / "model.safetensors").write_bytes(b"\x00" * 16)
        self.out = io.StringIO()
        # 登记自检会用登记里的解释器真 import 一遍依赖；测试用的 venv 没有 torch，所以拿假解释器顶上
        self.py = fake_python(self.tmp / "venv")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def do_register(self, **kw):
        kw.setdefault("model_dir", str(self.model_dir))
        kw.setdefault("model_name", "qwen3-asr-0.6b")
        kw.setdefault("port", 8790)
        kw.setdefault("cwd", str(PKG_ROOT))
        kw.setdefault("python_exe", self.py)
        return register(env=self.env, out=self.out, **kw)


class TestSelfCheck(RegistryCase):
    def test_passes_on_a_sane_setup(self):
        r = self_check(self.py, str(PKG_ROOT), str(self.model_dir))
        self.assertTrue(r.ok, r.problems)

    def test_missing_model_dir(self):
        r = self_check(sys.executable, str(PKG_ROOT), str(self.tmp / "nope"))
        self.assertFalse(r.ok)
        self.assertTrue(any("模型目录不存在" in p for p in r.problems))

    def test_model_dir_without_weights(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        (empty / "config.json").write_text("{}", encoding="utf-8")
        r = self_check(sys.executable, str(PKG_ROOT), str(empty))
        self.assertFalse(r.ok)
        self.assertTrue(any("权重" in p for p in r.problems))

    def test_cwd_without_package(self):
        r = self_check(sys.executable, str(self.tmp), str(self.model_dir))
        self.assertFalse(r.ok)
        self.assertTrue(any("ruyi_asr_shim" in p for p in r.problems))

    def test_imports_deps_with_the_registered_interpreter(self):
        """修前：自检只看路径，解释器里缺 torch 照样登记（如意每次拉起都是第一发请求 500）。"""
        r = self_check(self.py, str(PKG_ROOT), str(self.model_dir))
        self.assertTrue(r.ok, r.problems)
        args = fake_python_args(self.py)
        self.assertIn("-c", args)
        for mod in ("numpy", "transformers", "torch"):
            self.assertIn(mod, args, "自检要用登记里的解释器 import %s" % mod)

    def test_missing_dependency_fails(self):
        broken = fake_python(self.tmp / "broken-venv", exit_code=1,
                             stderr="ModuleNotFoundError: No module named torch")
        r = self_check(broken, str(PKG_ROOT), str(self.model_dir))
        self.assertFalse(r.ok)
        self.assertTrue(any("torch" in p and "install.ps1" in p for p in r.problems), r.problems)

    def test_real_interpreter_missing_module_fails(self):
        """真解释器、真子进程：清单里放一个不存在的模块，必须判不过。"""
        saved = registry_mod.IMPORT_CHECK
        registry_mod.IMPORT_CHECK = ("json", "ruyi_no_such_module_xyz")
        try:
            r = self_check(sys.executable, str(PKG_ROOT), str(self.model_dir))
        finally:
            registry_mod.IMPORT_CHECK = saved
        self.assertFalse(r.ok)
        self.assertTrue(any("ruyi_no_such_module_xyz" in p for p in r.problems), r.problems)

    def test_zero_byte_or_incomplete_weights_fail(self):
        """修前：权重 0 字节、目录里还躺着 .incomplete（下载中断）都照样登记。"""
        (self.model_dir / "model.safetensors").write_bytes(b"")
        self.assertTrue(any("0 字节" in p for p in model_dir_problems(str(self.model_dir))))
        self.assertFalse(self_check(self.py, str(PKG_ROOT), str(self.model_dir)).ok)
        (self.model_dir / "model.safetensors").write_bytes(b"\x00" * 16)
        (self.model_dir / "model-00002.safetensors.incomplete").write_bytes(b"\x00")
        self.assertTrue(any(".incomplete" in p for p in model_dir_problems(str(self.model_dir))))
        self.assertFalse(self_check(self.py, str(PKG_ROOT), str(self.model_dir)).ok)

    def test_sharded_index_must_be_complete(self):
        d = self.tmp / "sharded"
        d.mkdir()
        (d / "config.json").write_text("{}", encoding="utf-8")
        shards = ["model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"]
        (d / "model.safetensors.index.json").write_text(
            json.dumps({"metadata": {}, "weight_map": {"a.weight": shards[0], "b.weight": shards[1], "c.weight": shards[1]}}),
            encoding="utf-8")
        (d / shards[0]).write_bytes(b"\x00" * 8)
        problems = model_dir_problems(str(d))
        self.assertTrue(any(shards[1] in p for p in problems), problems)
        self.assertFalse(self_check(self.py, str(PKG_ROOT), str(d)).ok)
        (d / shards[1]).write_bytes(b"\x00" * 8)
        self.assertEqual(model_dir_problems(str(d)), [])
        self.assertTrue(self_check(self.py, str(PKG_ROOT), str(d)).ok)

    def test_relative_paths_rejected(self):
        r = self_check("python.exe", "asr-shim", str(self.model_dir))
        self.assertFalse(r.ok)
        self.assertGreaterEqual(len(r.problems), 2)


class TestRecord(RegistryCase):
    def test_shape_matches_convention(self):
        rec = build_record(
            python_exe=sys.executable, cwd=str(PKG_ROOT), model_dir=str(self.model_dir),
            model_name="qwen3-asr-0.6b", port=8790,
        )
        self.assertEqual(rec["schema"], 1)
        self.assertEqual(rec["id"], COMPONENT_ID)
        self.assertEqual(rec["kind"], "service")
        self.assertEqual(rec["version"], __version__)
        self.assertTrue(rec["name"])
        self.assertLessEqual(len(rec["name"]), 80)

        run = rec["run"]
        self.assertTrue(os.path.isabs(run["command"]))
        self.assertTrue(os.path.isabs(run["cwd"]))
        self.assertEqual(run["args"], ["-m", "ruyi_asr_shim"])
        self.assertLessEqual(len(run["args"]), 32)
        self.assertEqual(run["env"]["RUYI_ASR_MODEL_DIR"], str(self.model_dir))
        self.assertNotIn("RUYI_ASR_MODEL", run["env"], "缺省模型名不写进 env，少一处会过期的事实")
        for k, v in run["env"].items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, str)

        svc = rec["service"]
        self.assertEqual(svc["component"], COMPONENT_NAME_TAG)
        self.assertEqual(svc["portEnv"], "RUYI_ASR_PORT")
        self.assertEqual(svc["health"], "/health")
        self.assertEqual(svc["port"], 8790)

        self.assertEqual(rec["provides"], [
            {"type": "asr", "basePath": "/v1", "model": "qwen3-asr-0.6b", "protocol": "transcriptions"}
        ])
        self.assertRegex(rec["registeredAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_non_default_model_goes_into_env(self):
        rec = build_record(
            python_exe=sys.executable, cwd=str(PKG_ROOT), model_dir=str(self.model_dir),
            model_name="qwen3-asr-1.7b", port=8790,
        )
        self.assertEqual(rec["run"]["env"]["RUYI_ASR_MODEL"], "qwen3-asr-1.7b")
        self.assertEqual(rec["provides"][0]["model"], "qwen3-asr-1.7b")

    def test_no_secrets_in_record(self):
        rec = build_record(
            python_exe=sys.executable, cwd=str(PKG_ROOT), model_dir=str(self.model_dir),
            model_name="qwen3-asr-0.6b", port=8790,
        )
        blob = json.dumps(rec, ensure_ascii=False).lower()
        for bad in ("api_key", "apikey", "token", "secret", "password", "authorization"):
            self.assertNotIn(bad, blob)


class TestWrite(RegistryCase):
    def test_register_writes_utf8_no_bom_and_parses(self):
        self.assertEqual(self.do_register(), 0)
        path = registration_path(self.env)
        self.assertTrue(path.exists())
        raw = path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "登记文件必须是 UTF-8【无 BOM】")
        self.assertNotIn(b"\r\n", raw, "登记文件要 LF")
        rec = json.loads(raw.decode("utf-8"))
        self.assertEqual(rec["id"], COMPONENT_ID)
        self.assertIn("本地语音识别", rec["name"])

    def test_register_refuses_when_self_check_fails(self):
        rc = self.do_register(model_dir=str(self.tmp / "missing"))
        self.assertNotEqual(rc, 0)
        self.assertFalse(registration_path(self.env).exists(),
                         "自检不过就绝不许留下登记文件 —— 文件存在＝如意会去执行它")
        self.assertIn("模型目录不存在", self.out.getvalue())

    def test_register_refuses_when_deps_missing(self):
        broken = fake_python(self.tmp / "broken-venv", exit_code=1,
                             stderr="ModuleNotFoundError: No module named torch")
        rc = self.do_register(python_exe=broken)
        self.assertNotEqual(rc, 0)
        self.assertFalse(registration_path(self.env).exists(), "依赖 import 不了就不许登记")
        self.assertIn("torch", self.out.getvalue())

    def test_register_rejects_port_outside_1024_65535(self):
        """修前：--port 80 照登记，如意读登记时按 1024–65535 校验、把整份当成没装，静默跳过。"""
        for bad in (80, 0, 1023, 65536, -1):
            self.out = io.StringIO()
            self.assertNotEqual(self.do_register(port=bad), 0, bad)
            self.assertFalse(registration_path(self.env).exists(), bad)
            self.assertIn("1024", self.out.getvalue())
        for good in (1024, 65535):
            self.assertEqual(self.do_register(port=good), 0, good)
            self.assertEqual(json.loads(registration_path(self.env).read_text(encoding="utf-8"))["service"]["port"], good)

    def test_register_is_idempotent_and_overwrites(self):
        self.assertEqual(self.do_register(port=8790), 0)
        self.assertEqual(self.do_register(port=8999), 0)
        rec = json.loads(registration_path(self.env).read_text(encoding="utf-8"))
        self.assertEqual(rec["service"]["port"], 8999)

    def test_atomic_write_leaves_no_temp(self):
        self.assertEqual(self.do_register(), 0)
        leftovers = [p.name for p in self.components.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "原子写不许留临时文件")
        self.assertEqual(sorted(p.name for p in self.components.iterdir()),
                         [COMPONENT_ID + ".json"])

    def test_write_record_replaces_existing_atomically(self):
        path = self.components / "x.json"
        write_record({"a": 1}, path)
        write_record({"a": 2}, path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2})

    def test_unregister_removes_file(self):
        self.assertEqual(self.do_register(), 0)
        self.assertEqual(unregister(env=self.env, out=self.out), 0)
        self.assertFalse(registration_path(self.env).exists())

    def test_unregister_when_absent_is_success(self):
        self.assertEqual(unregister(env=self.env, out=self.out), 0)

    def test_does_not_touch_real_home(self):
        """跑完测试之后，用户真实主目录下不许多出东西。"""
        real = Path.home() / ".ruyi-toolbox" / "components" / (COMPONENT_ID + ".json")
        existed = real.exists()
        self.do_register()
        self.assertEqual(real.exists(), existed)


if __name__ == "__main__":
    unittest.main()
