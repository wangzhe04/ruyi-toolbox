"""PowerShell 脚本的编码判据。

**这条测试是被真实事故逼出来的**：开发过程中重写了一次 install.ps1，忘了补 BOM，
PowerShell 5.1 立刻把中文读成乱码并报一串 `Unexpected token`、整个脚本解析失败。
主仓也踩过同一个坑。所以把它钉成自动化判据，而不是靠人记住。
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

SCRIPTS = sorted((Path(__file__).resolve().parent.parent / "scripts").glob("*.ps1"))
BOM = b"\xef\xbb\xbf"


class TestPowerShellEncoding(unittest.TestCase):
    def test_scripts_exist(self):
        self.assertGreaterEqual(len(SCRIPTS), 4, "scripts/ 下应当有 install/download-model/start/make-sample")

    def test_utf8_bom(self):
        for p in SCRIPTS:
            raw = p.read_bytes()
            self.assertTrue(
                raw.startswith(BOM),
                "%s 缺 UTF-8 BOM —— Windows PowerShell 5.1 会把里面的中文读坏，整个脚本解析失败" % p.name,
            )

    def test_crlf_line_endings(self):
        for p in SCRIPTS:
            raw = p.read_bytes()[len(BOM):]
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""),
                             "%s 里有裸 LF，.ps1 要 CRLF" % p.name)

    def test_decodes_as_utf8(self):
        for p in SCRIPTS:
            p.read_bytes()[len(BOM):].decode("utf-8")  # 解不开就抛

    def test_no_powershell_7_only_syntax(self):
        """PowerShell 5.1 没有 && / || / 三元 / ?? —— 用了就是直接解析失败。"""
        for p in SCRIPTS:
            text = p.read_bytes()[len(BOM):].decode("utf-8")
            body = "\n".join(
                line for line in text.splitlines()
                if not line.strip().startswith("#") and "Write-Host" not in line
            )
            for bad in ("&&", "||", "??"):
                self.assertNotIn(bad, body, "%s 用了 PowerShell 5.1 没有的 %s" % (p.name, bad))


class TestDownloadModelCheck(unittest.TestCase):
    """download-model.ps1 下完之后的「下全了没有」：修前只查 config.json，下载中断的半截权重照样当成下好了去登记。
    修后调 registry.model_dir_problems（与 register 自检同一口径）。这里真跑脚本里内嵌的那段 Python。"""

    ROOT = Path(__file__).resolve().parent.parent

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asr-shim-dl-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def text(self) -> str:
        return (self.ROOT / "scripts" / "download-model.ps1").read_bytes().decode("utf-8-sig")

    def check_code(self) -> str:
        m = re.search(r"\$checkCode = @'\r?\n(.*?)\r?\n'@", self.text(), re.S)
        self.assertIsNotNone(m, "download-model.ps1 里没有 $checkCode 这段内嵌的 Python 检查")
        return m.group(1).replace("\r\n", "\n")

    def run_check(self, d: str):
        env = dict(os.environ, PYTHONPATH=str(self.ROOT), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-c", self.check_code(), d], cwd=self.tmp, env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)

    def make(self, name: str, files: dict) -> str:
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        for n, data in files.items():
            Path(d, n).write_bytes(data)
        return d

    def test_same_criteria_as_register(self):
        self.assertIn("model_dir_problems", self.text())
        self.assertEqual(self.run_check(self.make("ok", {"config.json": b"{}", "model.safetensors": b"x"})).returncode, 0)
        r = self.run_check(self.make("zero", {"config.json": b"{}", "model.safetensors": b""}))
        self.assertEqual(r.returncode, 1)
        self.assertIn("0 字节", r.stdout)
        self.assertEqual(self.run_check(self.make("noweights", {"config.json": b"{}"})).returncode, 1)

    def test_zero_byte_leftovers_are_removed_before_download(self):
        """重跑时把上次中断留下的 0 字节权重／配置删掉，下载器才会重下它（不然它按「文件在」跳过）。"""
        self.assertRegex(self.text(), r"Length -eq 0")


if __name__ == "__main__":
    unittest.main()
