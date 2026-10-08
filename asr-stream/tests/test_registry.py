"""登记：自检不过绝不写；写出来的形状与登记约定逐字段对；unregister 幂等；模型文件挑选优先 int8。"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ruyi_asr_stream import COMPONENT_ID, COMPONENT_NAME_TAG, __version__
from ruyi_asr_stream import registry as registry_mod
from ruyi_asr_stream.engine import resolve_model_files
from ruyi_asr_stream.registry import (
    DIR_ENV,
    build_record,
    register,
    registration_path,
    self_check,
    stream_model_problems,
    unregister,
)

from .helpers import fake_python, fake_python_args


def fake_model_dir(root: str, int8: bool = True) -> str:
    d = os.path.join(root, "model")
    os.makedirs(d, exist_ok=True)
    names = ["tokens.txt", "encoder-epoch-99-avg-1.onnx", "decoder-epoch-99-avg-1.onnx", "joiner-epoch-99-avg-1.onnx"]
    if int8:
        names += ["encoder-epoch-99-avg-1.int8.onnx", "joiner-epoch-99-avg-1.int8.onnx"]
    for n in names:
        Path(d, n).write_bytes(b"x")
    return d


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asr-stream-reg-")
        self.env = {DIR_ENV: os.path.join(self.tmp, "components")}
        self.pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 登记自检会用登记里的解释器真 import 一遍依赖；测试用的 venv 没有 sherpa_onnx，所以拿假解释器顶上
        self.py = fake_python(os.path.join(self.tmp, "venv"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolve_prefers_int8(self):
        d = fake_model_dir(self.tmp)
        f = resolve_model_files(d)
        self.assertTrue(f["encoder"].endswith("encoder-epoch-99-avg-1.int8.onnx"))
        self.assertTrue(f["decoder"].endswith("decoder-epoch-99-avg-1.onnx"))
        self.assertTrue(f["joiner"].endswith("joiner-epoch-99-avg-1.int8.onnx"))
        d2 = fake_model_dir(os.path.join(self.tmp, "fp"), int8=False)
        self.assertTrue(resolve_model_files(d2)["encoder"].endswith("encoder-epoch-99-avg-1.onnx"))
        os.unlink(os.path.join(d2, "tokens.txt"))
        with self.assertRaises(FileNotFoundError):
            resolve_model_files(d2)
        with self.assertRaises(FileNotFoundError):
            resolve_model_files(os.path.join(self.tmp, "nope"))

    def test_self_check_rejects(self):
        r = self_check("relative/python", os.path.join(self.tmp, "nocwd"), "")
        self.assertFalse(r.ok)
        self.assertEqual(len(r.problems), 3)
        r = self_check(sys.executable, self.pkg_root, os.path.join(self.tmp, "nomodel"))
        self.assertFalse(r.ok)

    def test_imports_deps_with_the_registered_interpreter(self):
        """修前：自检只看路径，解释器里缺 sherpa_onnx 照样登记、退出 0。"""
        d = fake_model_dir(self.tmp)
        self.assertTrue(self_check(self.py, self.pkg_root, d).ok)
        args = fake_python_args(self.py)
        self.assertIn("-c", args)
        for mod in ("numpy", "sherpa_onnx"):
            self.assertIn(mod, args, "自检要用登记里的解释器 import %s" % mod)
        broken = fake_python(os.path.join(self.tmp, "broken-venv"), exit_code=1,
                             stderr="ModuleNotFoundError: No module named sherpa_onnx")
        r = self_check(broken, self.pkg_root, d)
        self.assertFalse(r.ok)
        self.assertTrue(any("sherpa_onnx" in p and "install.ps1" in p for p in r.problems), r.problems)
        out = io.StringIO()
        self.assertEqual(register(model_dir=d, python_exe=broken, env=self.env, out=out), 1)
        self.assertFalse(registration_path(self.env).exists(), "依赖 import 不了就不许登记")
        self.assertIn("sherpa_onnx", out.getvalue())

    def test_real_interpreter_missing_module_fails(self):
        """真解释器、真子进程：清单里放一个不存在的模块，必须判不过。"""
        d = fake_model_dir(self.tmp)
        saved = registry_mod.IMPORT_CHECK
        registry_mod.IMPORT_CHECK = ("json", "ruyi_no_such_module_xyz")
        try:
            r = self_check(sys.executable, self.pkg_root, d)
        finally:
            registry_mod.IMPORT_CHECK = saved
        self.assertFalse(r.ok)
        self.assertTrue(any("ruyi_no_such_module_xyz" in p for p in r.problems), r.problems)

    def test_zero_byte_model_files_fail(self):
        """修前：encoder/decoder/joiner/tokens 为 0 字节（下载或解压中断）时 self_check 也 ok。"""
        for name in ("tokens.txt", "encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx", "joiner-epoch-99-avg-1.int8.onnx"):
            root = os.path.join(self.tmp, "z-" + name)
            d = fake_model_dir(root)
            Path(d, name).write_bytes(b"")
            self.assertTrue(any(name in p for p in stream_model_problems(d)), name)
            self.assertFalse(self_check(self.py, self.pkg_root, d).ok, name)
        self.assertEqual(stream_model_problems(fake_model_dir(os.path.join(self.tmp, "good"))), [])

    def test_register_refuses_without_model(self):
        out = io.StringIO()
        code = register(model_dir=os.path.join(self.tmp, "missing"), env=self.env, out=out)
        self.assertEqual(code, 1)
        self.assertFalse(registration_path(self.env).exists())
        self.assertIn("自检没过", out.getvalue())

    def test_register_writes_contract_shape(self):
        d = fake_model_dir(self.tmp)
        out = io.StringIO()
        self.assertEqual(register(model_dir=d, port=8791, python_exe=self.py, env=self.env, out=out), 0)
        p = registration_path(self.env)
        self.assertEqual(p.name, COMPONENT_ID + ".json")
        raw = p.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "登记文件不许带 BOM")
        self.assertNotIn(b"\r\n", raw, "登记文件必须是 LF")
        rec = json.loads(raw.decode("utf-8"))
        self.assertEqual(rec["schema"], 1)
        self.assertEqual(rec["id"], COMPONENT_ID)
        self.assertEqual(rec["kind"], "service")
        self.assertEqual(rec["version"], __version__)
        self.assertEqual(rec["run"]["command"], os.path.abspath(self.py))
        self.assertEqual(rec["run"]["args"], ["-m", "ruyi_asr_stream"])
        self.assertEqual(rec["run"]["cwd"], self.pkg_root)
        self.assertEqual(rec["run"]["env"], {"RUYI_ASR_STREAM_MODEL_DIR": d})
        self.assertEqual(rec["service"], {"port": 8791, "portEnv": "RUYI_ASR_STREAM_PORT", "health": "/health", "component": COMPONENT_NAME_TAG})
        self.assertEqual(rec["provides"], [{"type": "asr-stream", "basePath": "/v1", "model": "zipformer-bilingual-zh-en"}])
        self.assertTrue(rec["registeredAt"].endswith("Z"))
        self.assertFalse(any(n.endswith(".tmp") for n in os.listdir(p.parent)), "临时文件要 rename 走")

    def test_build_record_custom_model_name_goes_to_env(self):
        rec = build_record(python_exe="C:\\py.exe", cwd="C:\\x", model_dir="C:\\m", model_name="zh-only", port=8791)
        self.assertEqual(rec["run"]["env"]["RUYI_ASR_STREAM_MODEL"], "zh-only")
        self.assertEqual(rec["provides"][0]["model"], "zh-only")

    def test_cli_register_survives_non_numeric_port_env(self):
        """修前：RUYI_ASR_STREAM_PORT 不是数字时，register 子命令在算 --port 缺省值那一步直接抛 ValueError 栈。"""
        from unittest import mock

        from ruyi_asr_stream.__main__ import main

        d = fake_model_dir(self.tmp)
        with mock.patch.dict(os.environ, {"RUYI_ASR_STREAM_PORT": "abc", DIR_ENV: self.env[DIR_ENV]}):
            self.assertEqual(main(["register", "--model-dir", d, "--python", self.py]), 0)
        rec = json.loads(registration_path(self.env).read_text("utf-8"))
        self.assertEqual(rec["service"]["port"], 8791, "看不懂的端口环境变量回落缺省")

    def test_register_rejects_port_outside_1024_65535(self):
        """修前：--port 80 照登记，如意读登记时按 1024–65535 校验、把整份当成没装，静默跳过。"""
        d = fake_model_dir(self.tmp)
        for bad in (80, 0, 1023, 65536, -1):
            out = io.StringIO()
            self.assertEqual(register(model_dir=d, port=bad, python_exe=self.py, env=self.env, out=out), 1, bad)
            self.assertFalse(registration_path(self.env).exists(), bad)
            self.assertIn("1024", out.getvalue())
        for good in (1024, 65535):
            self.assertEqual(register(model_dir=d, port=good, python_exe=self.py, env=self.env, out=io.StringIO()), 0, good)
            self.assertEqual(json.loads(registration_path(self.env).read_text("utf-8"))["service"]["port"], good)

    def test_unregister_idempotent(self):
        d = fake_model_dir(self.tmp)
        self.assertEqual(register(model_dir=d, python_exe=self.py, env=self.env, out=io.StringIO()), 0)
        self.assertEqual(unregister(env=self.env, out=io.StringIO()), 0)
        self.assertFalse(registration_path(self.env).exists())
        self.assertEqual(unregister(env=self.env, out=io.StringIO()), 0)


if __name__ == "__main__":
    unittest.main()
