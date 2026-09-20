"""PowerShell 脚本的编码判据。

**这条测试是被真实事故逼出来的**：开发过程中重写了一次 install.ps1，忘了补 BOM，
PowerShell 5.1 立刻把中文读成乱码并报一串 `Unexpected token`、整个脚本解析失败。
主仓也踩过同一个坑。所以把它钉成自动化判据，而不是靠人记住。
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
