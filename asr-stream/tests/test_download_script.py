"""scripts/download-model.ps1 的判据（PowerShell 本身只在 Windows 上跑，这里在任何平台上查文本 + 真跑它内嵌的 Python 检查）。

修前两处坑：
  · 以 tokens.txt 存在为「已下载」并跳过整段 —— 下载或解压中断留下的半截目录永远不会被重下；
  · curl 直接写最终文件名 —— 中途断网留下的半截文件顶着正式文件名。
修后：所有 curl 都写 <文件>.part、完整了才改名；「已下载」判据与 register 自检同一口径（registry 的
stream_model_problems / offline_model_problems：必需文件个个在、非零字节）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "download-model.ps1"


def _text() -> str:
    return SCRIPT.read_bytes().decode("utf-8-sig")


def _check_code() -> str:
    m = re.search(r"\$checkCode = @'\r?\n(.*?)\r?\n'@", _text(), re.S)
    if m is None:
        raise AssertionError("download-model.ps1 里没有 $checkCode 这段内嵌的 Python 检查")
    return m.group(1).replace("\r\n", "\n")


class DownloadScriptTextTest(unittest.TestCase):
    def test_every_curl_writes_to_part_file(self):
        lines = [ln for ln in _text().splitlines() if "curl.exe" in ln and not ln.lstrip().startswith("#")]
        self.assertGreaterEqual(len(lines), 3)
        for ln in lines:
            m = re.search(r"\s-o\s+(.*)$", ln)     # -o 之后（目标 + URL；URL 里不会有 .part）
            self.assertIsNotNone(m, ln)
            self.assertRegex(m.group(1), r"\.part|\$part", "curl 要先写 <文件>.part，下完整了才改名：" + ln.strip())

    def test_downloaded_means_all_required_files_nonempty(self):
        text = _text()
        self.assertNotRegex(text, r'Test-Path \(Join-Path \$(Dest|offlineDir) "tokens\.txt"\)',
                            "不能再拿 tokens.txt 在不在当「已下载」")
        self.assertIn("stream_model_problems", text, "「已下载」要与 register 自检同一口径")
        self.assertIn("offline_model_problems", text)

    def test_model_names_are_validated_before_used_as_paths(self):
        """半截目录会被整个删掉重下，所以拼路径用的模型名不许是 .. 或空。"""
        self.assertEqual(len(re.findall(r"ValidatePattern\(", _text())), 2)


class EmbeddedCheckTest(unittest.TestCase):
    """真跑脚本里内嵌的那段 Python（与 PowerShell 调它的方式一样：python -c <code> <kind> <dir>）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asr-stream-dl-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_check(self, kind: str, d: str):
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-c", _check_code(), kind, d], cwd=self.tmp, env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)

    def make(self, name: str, files: dict) -> str:
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        for n, data in files.items():
            Path(d, n).write_bytes(data)
        return d

    def test_stream(self):
        good = {"tokens.txt": b"x", "encoder-e.int8.onnx": b"x", "decoder-e.onnx": b"x", "joiner-e.int8.onnx": b"x"}
        self.assertEqual(self.run_check("stream", self.make("ok", good)).returncode, 0)
        half = dict(good, **{"joiner-e.int8.onnx": b""})
        r = self.run_check("stream", self.make("half", half))
        self.assertEqual(r.returncode, 1)
        self.assertIn("joiner-e.int8.onnx", r.stdout)
        part_only = dict(good)
        part_only.pop("encoder-e.int8.onnx")
        part_only["encoder-e.int8.onnx.part"] = b"x"     # 只有 .part = 没下完
        self.assertEqual(self.run_check("stream", self.make("part", part_only)).returncode, 1)
        self.assertEqual(self.run_check("stream", os.path.join(self.tmp, "nope")).returncode, 1)

    def test_offline(self):
        self.assertEqual(self.run_check("offline", self.make("sv", {"tokens.txt": b"x", "model.int8.onnx": b"x"})).returncode, 0)
        self.assertEqual(self.run_check("offline", self.make("sv0", {"tokens.txt": b"x", "model.int8.onnx": b""})).returncode, 1)


if __name__ == "__main__":
    unittest.main()
