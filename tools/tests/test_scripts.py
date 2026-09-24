"""仓库里 .ps1／.cmd 脚本的体检 + package-bundle.ps1 进度/执行器/压缩逻辑的测试（后者用 PowerShell 跑）。

只在 Windows 上有意义（脚本本来就是给 Windows PowerShell 5.1 写的）；别的平台整个跳过。

跑法（仓库根目录）：  python -m unittest discover -s tools/tests
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("powershell")
WINDOWS = sys.platform == "win32" and POWERSHELL is not None


def _scripts():
    skip = {"models", "samples", ".git", "__pycache__", "dist"}
    out = []
    for p in ROOT.rglob("*"):
        parts = p.relative_to(ROOT).parts
        # .venv 之外还有 .venv-rocm 这类并列环境（打包器同样按 .venv* 排除），里面的 activate.ps1 不归本仓管
        if p.suffix.lower() in (".ps1", ".cmd") and not (set(parts) & skip) and not any(x.startswith(".venv") for x in parts):
            out.append(p)
    return sorted(out)


@unittest.skipUnless(WINDOWS, "需要 Windows PowerShell")
class ScriptHygieneTest(unittest.TestCase):
    def test_there_are_scripts_to_check(self):
        self.assertGreaterEqual(len(_scripts()), 8)

    def test_ps1_are_utf8_bom_crlf_without_stray_cr(self):
        # 约定见根 README：.ps1 一律 UTF-8 with BOM + CRLF（5.1 读无 BOM 的 UTF-8 会把中文读坏）。
        # 孤立回车符（\r 后面不是 \n）通常是路径里的 "\r..." 被转义成了回车——输出里路径会被断成两行。
        for p in _scripts():
            raw = p.read_bytes()
            if p.suffix.lower() == ".ps1":
                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), f"{p.relative_to(ROOT)} 没有 UTF-8 BOM")
            text = raw.decode("utf-8-sig")
            self.assertEqual(len(re.findall(r"(?<!\r)\n", text)), 0, f"{p.relative_to(ROOT)} 里有只有 LF 的换行")
            self.assertEqual(len(re.findall(r"\r(?!\n)", text)), 0, f"{p.relative_to(ROOT)} 里有孤立的回车符")

    def test_ps1_parse_without_errors(self):
        for p in _scripts():
            if p.suffix.lower() != ".ps1":
                continue
            cmd = (
                "$e=$null;$t=$null;"
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{p}',[ref]$t,[ref]$e);"
                "if($e.Count){$e|ForEach-Object{'行 '+$_.Extent.StartLineNumber+': '+$_.Message};exit 1}"
            )
            r = subprocess.run([POWERSHELL, "-NoProfile", "-Command", cmd], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(r.returncode, 0, f"{p.relative_to(ROOT)} 语法有错：\n{r.stdout}{r.stderr}")

    def test_setup_script_text_embedded_in_bundle_parses(self):
        # package-bundle.ps1 里有一大段 here-string 是"生成的 setup.ps1"，它在打包时才被写成文件，
        # 平时没人解析它——这里把它取出来单独解析一遍，免得改坏了要到目标机器上才发现。
        cmd = (
            f". '{ROOT / 'tools' / 'package-bundle.ps1'}';"
            "$s = Get-SetupScriptText;$e=$null;$t=$null;"
            "[void][System.Management.Automation.Language.Parser]::ParseInput($s,[ref]$t,[ref]$e);"
            "if($e.Count){$e|ForEach-Object{'行 '+$_.Extent.StartLineNumber+': '+$_.Message};exit 1}"
        )
        r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, f"生成的 setup.ps1 语法有错：\n{r.stdout}{r.stderr}")


@unittest.skipUnless(WINDOWS, "需要 Windows PowerShell")
class BundleLogicTest(unittest.TestCase):
    def test_progress_runner_copy_zip_preflight(self):
        script = ROOT / "tools" / "tests" / "progress_tests.ps1"
        r = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        self.assertEqual(r.returncode, 0, f"progress_tests.ps1 有 {r.returncode} 项失败：\n{r.stdout}\n{r.stderr}")
        self.assertIn("0 失败", r.stdout)


if __name__ == "__main__":
    unittest.main()
