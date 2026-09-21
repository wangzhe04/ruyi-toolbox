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
from ruyi_asr_stream.engine import resolve_model_files
from ruyi_asr_stream.registry import DIR_ENV, build_record, register, registration_path, self_check, unregister


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

    def test_register_refuses_without_model(self):
        out = io.StringIO()
        code = register(model_dir=os.path.join(self.tmp, "missing"), env=self.env, out=out)
        self.assertEqual(code, 1)
        self.assertFalse(registration_path(self.env).exists())
        self.assertIn("自检没过", out.getvalue())

    def test_register_writes_contract_shape(self):
        d = fake_model_dir(self.tmp)
        out = io.StringIO()
        self.assertEqual(register(model_dir=d, port=8791, env=self.env, out=out), 0)
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
        self.assertEqual(rec["run"]["command"], os.path.abspath(sys.executable))
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

    def test_unregister_idempotent(self):
        d = fake_model_dir(self.tmp)
        register(model_dir=d, env=self.env, out=io.StringIO())
        self.assertEqual(unregister(env=self.env, out=io.StringIO()), 0)
        self.assertFalse(registration_path(self.env).exists())
        self.assertEqual(unregister(env=self.env, out=io.StringIO()), 0)


if __name__ == "__main__":
    unittest.main()
