"""tools/localwheels.py 的单测。不需要网络、不需要 uv、不需要真的 venv —— 在临时目录里现造一个假的
site-packages 与 dist-info，验证"要求满不满足"的判断、以及还原出来的轮子内容与 RECORD 哈希对得上。

跑法（仓库根目录）：  python -m unittest discover -s tools/tests
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import localwheels as lw  # noqa: E402


class SatisfiesTest(unittest.TestCase):
    def test_common_specifiers(self):
        self.assertTrue(lw.satisfies("5.17.0", ">=5.13.0"))
        self.assertTrue(lw.satisfies("5.13.0", ">=5.13.0"))
        self.assertFalse(lw.satisfies("5.12.9", ">=5.13.0"))
        self.assertTrue(lw.satisfies("2.5.3", ">=1.24"))
        self.assertTrue(lw.satisfies("1.15.0", ">=0.30"))
        self.assertTrue(lw.satisfies("78.1.0", ">=68"))

    def test_compound_and_other_operators(self):
        self.assertTrue(lw.satisfies("1.5", ">=1.2,<2"))
        self.assertFalse(lw.satisfies("2.0", ">=1.2,<2"))
        self.assertTrue(lw.satisfies("1.2.3", "==1.2.*"))
        self.assertFalse(lw.satisfies("1.3.0", "==1.2.*"))
        self.assertTrue(lw.satisfies("1.4.2", "~=1.4"))
        self.assertFalse(lw.satisfies("2.0", "~=1.4"))
        self.assertTrue(lw.satisfies("1.0", "!=2.0"))

    def test_empty_specifier_means_any_version(self):
        self.assertTrue(lw.satisfies("0.0.1", ""))

    def test_local_version_and_prerelease_suffix_are_ignored(self):
        self.assertTrue(lw.satisfies("2.11.0+cu128", ">=2.5"))
        self.assertTrue(lw.satisfies("1.0rc1", ">=1.0"))

    def test_unparseable_is_treated_as_unsatisfied(self):
        # 认不出就当"不满足"——宁可多下一个，也不能把不对的当成本机有。
        self.assertFalse(lw.satisfies("1.0", "===weird"))
        self.assertFalse(lw.satisfies("", ">=1"))


class RequirementTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(lw.parse_requirement("transformers>=5.13.0"), ("transformers", ">=5.13.0"))
        self.assertEqual(lw.parse_requirement("torch"), ("torch", ""))
        self.assertEqual(lw.parse_requirement("uvicorn[standard]>=0.30 ; python_version>'3.9'"), ("uvicorn", ">=0.30"))

    def test_norm(self):
        self.assertEqual(lw.norm("sherpa_onnx"), "sherpa-onnx")
        self.assertEqual(lw.norm("Sherpa.Onnx"), "sherpa-onnx")

    def test_torch_variant(self):
        self.assertEqual(lw.torch_variant("2.11.0+cu128"), "nvidia")
        self.assertEqual(lw.torch_variant("2.11.0+cpu"), "cpu")
        self.assertEqual(lw.torch_variant("2.9.1+rocm7.2.1"), "amd")
        self.assertEqual(lw.torch_variant("2.11.0"), "unknown")


def _make_installed(site: Path, name="foo", version="1.0", tag="py3-none-any", direct_url=None):
    """在 site 下造一个"已安装"的包：foo/ + foo-1.0.dist-info/，RECORD 里还混进 pyc 与 Scripts 下的入口脚本。"""
    pkg = site / name
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    (pkg / "data.bin").write_bytes(bytes(range(256)) * 40)
    (pkg / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"\x00pyc")
    di = site / f"{name}-{version}.dist-info"
    di.mkdir()
    (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    (di / "WHEEL").write_text(f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n", encoding="utf-8")
    (di / "INSTALLER").write_text("uv\n", encoding="utf-8")
    (di / "top_level.txt").write_text(name + "\n", encoding="utf-8")
    if direct_url:
        (di / "direct_url.json").write_text(json.dumps(direct_url), encoding="utf-8")
    rows = [
        f"{name}/__init__.py,sha256=x,11",
        f"{name}/data.bin,sha256=x,10240",
        f"{name}/__pycache__/__init__.cpython-312.pyc,sha256=x,4",
        f"{di.name}/METADATA,sha256=x,1",
        f"{di.name}/WHEEL,sha256=x,1",
        f"{di.name}/INSTALLER,sha256=x,3",
        f"{di.name}/top_level.txt,sha256=x,4",
        f"../../Scripts/{name}.exe,sha256=x,9",
        f"{di.name}/RECORD,,",
    ]
    (di / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    # importlib.metadata 会丢掉"RECORD 里有、磁盘上没有"的文件，所以入口脚本得真的存在
    # （真 venv 里 Scripts 目录就在 site-packages 的 ../../ 处）。
    if site.name == "site-packages":
        scripts = site.parent.parent / "Scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / f"{name}.exe").write_bytes(b"MZ")
    return di


def _rec_for(di: Path):
    dist = metadata.PathDistribution(di)
    return {"name": dist.metadata["Name"], "norm": lw.norm(dist.metadata["Name"]), "version": dist.version,
            "distinfo": di, "dist": dist, "tag": lw._read_wheel_tag(di), "skip": ""}


class BuildWheelTest(unittest.TestCase):
    def test_roundtrip_content_and_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "Lib" / "site-packages"
            site.mkdir(parents=True)
            di = _make_installed(site)
            out = Path(tmp) / "out"
            out.mkdir()
            filename, wheel_bytes, raw_bytes, nfiles, outside = lw.build_wheel(_rec_for(di), str(out), 6)

            self.assertEqual(filename, "foo-1.0-py3-none-any.whl")
            self.assertTrue((out / filename).is_file())
            self.assertFalse(list(out.glob("*.part")), "写完不该留下 .part")
            self.assertEqual(outside, ["../../Scripts/foo.exe"])  # 入口脚本不进轮子，由安装器按 entry_points 重新生成

            with zipfile.ZipFile(out / filename) as zf:
                names = set(zf.namelist())
                self.assertEqual(
                    names,
                    {"foo/__init__.py", "foo/data.bin", "foo-1.0.dist-info/METADATA", "foo-1.0.dist-info/WHEEL",
                     "foo-1.0.dist-info/top_level.txt", "foo-1.0.dist-info/RECORD"},
                )  # 没有 pyc、没有 INSTALLER
                self.assertIsNone(zf.testzip())
                # RECORD 里每一行的哈希与大小都要和轮子里的真实内容对得上（安装器会校验）
                rows = [r for r in zf.read("foo-1.0.dist-info/RECORD").decode().splitlines() if r]
                seen = set()
                for row in rows:
                    path, h, size = row.split(",")
                    seen.add(path)
                    if path.endswith("/RECORD"):
                        self.assertEqual((h, size), ("", ""))
                        continue
                    data = zf.read(path)
                    want = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                    self.assertEqual(h, want, path)
                    self.assertEqual(int(size), len(data), path)
                self.assertEqual(seen, names)

    def test_large_content_is_streamed_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "Lib" / "site-packages"
            site.mkdir(parents=True)
            di = _make_installed(site)
            big = os.urandom(3 * 1024 * 1024 + 123)  # 跨过多个 1 MB 的读块
            (site / "foo" / "data.bin").write_bytes(big)
            out = Path(tmp) / "out"
            out.mkdir()
            filename, *_ = lw.build_wheel(_rec_for(di), str(out), 1)
            with zipfile.ZipFile(out / filename) as zf:
                self.assertEqual(zf.read("foo/data.bin"), big)

    def test_platform_tag_goes_into_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "Lib" / "site-packages"
            site.mkdir(parents=True)
            di = _make_installed(site, name="numpyish", version="2.5.3", tag="cp312-cp312-win_amd64")
            out = Path(tmp) / "out"
            out.mkdir()
            filename, *_ = lw.build_wheel(_rec_for(di), str(out), 1)
            self.assertEqual(filename, "numpyish-2.5.3-cp312-cp312-win_amd64.whl")


class ProgressTest(unittest.TestCase):
    def test_start_and_finish_lines(self):
        import io
        buf = io.StringIO()
        p = lw.Progress(1000, stream=buf)
        p.start()
        p.finish()
        self.assertEqual(buf.getvalue().splitlines(), ["PROG 0 1000", "PROG 1000 1000"])

    def test_add_is_throttled_but_done_is_exact(self):
        import io
        buf = io.StringIO()
        p = lw.Progress(10_000, stream=buf)
        for _ in range(100):  # 一瞬间加 100 次：不该刷出 100 行
            p.add(100)
        self.assertLess(len(buf.getvalue().splitlines()), 5)
        self.assertEqual(p.done, 10_000)

    def test_zero_total_does_not_divide_by_zero_downstream(self):
        import io
        buf = io.StringIO()
        lw.Progress(0, stream=buf).finish()
        self.assertEqual(buf.getvalue().strip(), "PROG 1 1")  # 分母至少为 1，界面那头不会除零

    def test_build_wheel_reports_exactly_the_raw_bytes(self):
        import io
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "Lib" / "site-packages"
            site.mkdir(parents=True)
            di = _make_installed(site)
            rec = _rec_for(di)
            entries, _ = lw.gather_entries(rec)
            expect = sum(src.stat().st_size for _, src in entries)
            self.assertGreater(expect, 10240)  # 至少含 data.bin 与 __init__.py
            out = Path(tmp) / "out"
            out.mkdir()
            buf = io.StringIO()
            prog = lw.Progress(expect, stream=buf)
            filename, wheel_bytes, raw_bytes, nfiles, outside = lw.build_wheel(
                rec, str(out), 1, gathered=(entries, []), progress=prog
            )
            self.assertEqual(prog.done, expect)   # 汇报的字节数与真实读到的一致
            self.assertEqual(raw_bytes, expect)
            self.assertEqual(nfiles, len(entries))

    def test_gather_entries_skips_pyc_installer_and_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "Lib" / "site-packages"
            site.mkdir(parents=True)
            di = _make_installed(site)
            entries, outside = lw.gather_entries(_rec_for(di))
            names = {rel for rel, _ in entries}
            self.assertIn("foo/__init__.py", names)
            self.assertNotIn("foo/__pycache__/__init__.cpython-312.pyc", names)
            self.assertNotIn("foo-1.0.dist-info/INSTALLER", names)
            self.assertNotIn("foo-1.0.dist-info/RECORD", names)   # RECORD 是重新生成的，不照搬
            self.assertEqual(outside, ["../../Scripts/foo.exe"])


class WheelTagTest(unittest.TestCase):
    def _distinfo(self, tmp, *tags):
        di = Path(tmp) / "x-1.0.dist-info"
        di.mkdir()
        (di / "WHEEL").write_text("Wheel-Version: 1.0\n" + "".join(f"Tag: {t}\n" for t in tags), encoding="utf-8")
        return di

    def test_multiple_tag_lines_become_a_compressed_tag_set(self):
        # colorama／shellingham／soundfile 都是这种：py2 与 py3 两行。文件名只写 py2 会被安装器当成 Python 2 专用而忽略。
        with tempfile.TemporaryDirectory() as tmp:
            di = self._distinfo(tmp, "py2-none-any", "py3-none-any")
            self.assertEqual(lw._read_wheel_tag(di), "py2.py3-none-any")

    def test_platform_specific_multi_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            di = self._distinfo(tmp, "py2-none-win_amd64", "py3-none-win_amd64")
            self.assertEqual(lw._read_wheel_tag(di), "py2.py3-none-win_amd64")

    def test_single_tag_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            di = self._distinfo(tmp, "cp312-cp312-win_amd64")
            self.assertEqual(lw._read_wheel_tag(di), "cp312-cp312-win_amd64")

    def test_no_tag_or_malformed_gives_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(lw._read_wheel_tag(self._distinfo(tmp)), "")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(lw._read_wheel_tag(self._distinfo(tmp, "garbage")), "")


class ProjectItselfTest(unittest.TestCase):
    def test_editable_and_local_dir_installs_are_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            di = _make_installed(site, direct_url={"url": "file:///E:/x", "dir_info": {"editable": True}})
            self.assertTrue(lw._is_project_itself(di))

    def test_normal_pypi_install_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            di = _make_installed(site)
            self.assertFalse(lw._is_project_itself(di))


if __name__ == "__main__":
    unittest.main()
