"""把一个已装好的 venv 里的包"还原"成 wheel 文件 —— 给 package-bundle.ps1 打离线依赖库用。

为什么要有它：打离线依赖库以前是让系统 pip 重新联网解析、下载一遍。那样有两个毛病 ——
一是慢、遇到代理挂住就没有尽头；二是 pip 按【它自己那个 Python】挑轮子（系统是 3.13 就下 cp313），
而各组件的 venv 是 3.12，离线装的时候根本装不上。本机这个 venv 已经是装好、跑通过的一套（含 CUDA 版
torch），直接把它还原成轮子，就不用下、也不会错位。只有本机没有的（比如换一种显卡的 torch）才需要下。

必须用【组件自己 venv 的 python】来跑（importlib.metadata 看的就是运行它的那个环境）。只用标准库。

  python localwheels.py plan   [--spec "a>=1" --spec b ...] [--exclude a,b] [--sizes]
  python localwheels.py export --out DIR [--exclude a,b] [--level 6]

plan   ：列出本机 venv 里装了什么、能不能还原成轮子，以及每条 --spec 要求本机满不满足。
export ：把能还原的包写成 DIR 下的 .whl，逐个包打一行结果（OK／SKIP／FAIL），另外每 ~0.4 秒打一行
         "PROG 已写字节 总字节"（进度条用）；有 FAIL 就返回 1。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from importlib import metadata

_SKIP_IN_DISTINFO = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "RECORD.jws", "RECORD.p7s"}
_CHUNK = 1024 * 1024


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ── 版本与要求的比较（只覆盖本仓 pyproject 里会写的那几种；认不出就当"不满足"，宁可多下一个）──────────
def parse_version(v: str):
    v = v.split("+", 1)[0]
    nums = []
    for seg in v.split("."):
        m = re.match(r"\d+", seg)
        if not m:
            break
        nums.append(int(m.group()))
        if m.end() != len(seg):  # 1.0rc1 之类：数字部分收下，后缀不管
            break
    return tuple(nums)


def _cmp(a, b):
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def satisfies(installed_version: str, specifier: str) -> bool:
    """installed_version 满不满足像 '>=1.2,<2' 这样的限定。空限定＝任何版本都行。"""
    specifier = specifier.strip()
    if not specifier:
        return True
    have = parse_version(installed_version)
    if not have:
        return False
    for part in specifier.split(","):
        m = re.match(r"^\s*(>=|<=|==|!=|~=|>|<)\s*([0-9][^\s]*)\s*$", part)
        if not m:
            return False
        op, want_s = m.groups()
        if op == "==" and want_s.endswith(".*"):
            want = parse_version(want_s[:-2])
            if have[: len(want)] != want:
                return False
            continue
        want = parse_version(want_s)
        if not want:
            return False
        c = _cmp(have, want)
        ok = {
            ">=": c >= 0, "<=": c <= 0, "==": c == 0, "!=": c != 0, ">": c > 0, "<": c < 0,
            "~=": c >= 0 and have[: max(len(want) - 1, 1)] == want[: max(len(want) - 1, 1)],
        }[op]
        if not ok:
            return False
    return True


def parse_requirement(req: str):
    """'transformers>=5.13.0' -> ('transformers', '>=5.13.0')；去掉 extras 与环境标记。"""
    req = req.split(";", 1)[0].strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$", req)
    if not m:
        return req, ""
    return m.group(1), m.group(3).strip().lstrip("(").rstrip(")")


# ── 读已装的包 ───────────────────────────────────────────────────────────────────────────
def _read_wheel_tag(distinfo) -> str:
    """轮子文件名里的标签部分，如 'cp312-cp312-win_amd64'、'py2.py3-none-any'。

    WHEEL 里一个轮子可以有好几行 Tag（colorama 是 py2-none-any 与 py3-none-any 两行）。文件名里必须写成
    规范的"压缩标签集"——各段用 '.' 连起来（py2.py3-none-any）。只取第一行就会写成 py2-none-any，
    安装器认文件名，会把它当成"Python 2 专用"直接忽略，这个包离线时就装不上了。
    """
    wheel = distinfo / "WHEEL"
    if not wheel.is_file():
        return ""
    pys, abis, plats = [], [], []
    for line in wheel.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("Tag:"):
            continue
        parts = line.split(":", 1)[1].strip().split("-")
        if len(parts) != 3:
            continue
        for bucket, part in zip((pys, abis, plats), parts):
            if part not in bucket:
                bucket.append(part)
    if not pys:
        return ""
    return "-".join((".".join(pys), ".".join(abis), ".".join(plats)))


def _is_project_itself(distinfo) -> bool:
    """editable / 本地目录装的项目本体（比如 asr-shim 自己）—— 不是能分发的轮子，install.ps1 会自己装。"""
    du = distinfo / "direct_url.json"
    if not du.is_file():
        return False
    try:
        data = json.loads(du.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return "dir_info" in data or str(data.get("url", "")).startswith("file:")


def inspect_dists():
    out = []
    for dist in metadata.distributions():
        distinfo = getattr(dist, "_path", None)
        name = dist.metadata["Name"] if dist.metadata else None
        if distinfo is None or not name:
            continue
        rec = {
            "name": name,
            "norm": norm(name),
            "version": dist.version,
            "distinfo": distinfo,
            "dist": dist,
            "tag": _read_wheel_tag(distinfo),
            "skip": "",
        }
        if _is_project_itself(distinfo):
            rec["skip"] = "项目本体（install.ps1 会自己装）"
        elif not (distinfo / "RECORD").is_file() or not dist.files:
            rec["skip"] = "没有 RECORD，还原不了"
        elif not rec["tag"]:
            rec["skip"] = "没有 WHEEL 元数据，认不出平台标签"
        out.append(rec)
    # 同名重复（不该有）只留第一个
    seen, uniq = set(), []
    for r in out:
        if r["norm"] in seen:
            continue
        seen.add(r["norm"])
        uniq.append(r)
    return sorted(uniq, key=lambda r: r["norm"])


def torch_variant(version: str) -> str:
    if "+cu" in version:
        return "nvidia"
    if "+rocm" in version:
        return "amd"
    if "+cpu" in version:
        return "cpu"
    return "unknown"


def cmd_plan(args) -> int:
    dists = inspect_dists()
    by_norm = {d["norm"]: d for d in dists}
    specs = []
    for raw in [s for s in (args.spec or []) if s.strip()]:
        name, spec = parse_requirement(raw)
        d = by_norm.get(norm(name))
        if d is None or d["skip"]:
            status, have = "missing", (d["version"] if d else "")
        elif satisfies(d["version"], spec):
            status, have = "local", d["version"]
        else:
            status, have = "missing", d["version"]
        specs.append({"spec": raw.strip(), "name": name, "status": status, "localVersion": have})
    exclude = {norm(x) for x in (args.exclude or "").split(",") if x.strip()}
    packages = []
    for d in dists:
        item = {"name": d["name"], "version": d["version"], "tag": d["tag"], "skip": d["skip"],
                "excluded": d["norm"] in exclude}
        if args.sizes:  # 要 stat 每一个文件（torch 上万个），界面上随手切换时不必付这个代价，所以是可选的
            item["bytes"] = 0 if d["skip"] else sum(src.stat().st_size for _, src in gather_entries(d)[0])
        packages.append(item)
    result = {
        "python": ".".join(map(str, sys.version_info[:3])),
        "pyTag": f"cp{sys.version_info[0]}{sys.version_info[1]}",
        "torchVariant": torch_variant(by_norm["torch"]["version"]) if "torch" in by_norm else "",
        "packages": packages,
        "specs": specs,
    }
    print(json.dumps(result))  # 默认 ensure_ascii：管道另一头（PowerShell）不用操心代码页
    return 0


# ── 还原成轮子 ───────────────────────────────────────────────────────────────────────────
def _record_hash(h) -> str:
    return "sha256=" + base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode("ascii")


def gather_entries(rec):
    """这个包里哪些文件要进轮子。返回 (entries[(相对路径, 源文件 Path)], outside[落在 site-packages 之外的相对路径])。"""
    dist, distinfo = rec["dist"], rec["distinfo"]
    site = distinfo.parent
    entries, outside = [], []
    for f in dist.files:
        rel = str(f).replace("\\", "/")
        if rel.startswith("../") or os.path.isabs(rel):
            outside.append(rel)
            continue
        base = rel.rsplit("/", 1)[-1]
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        if rel.startswith(distinfo.name + "/") and base in _SKIP_IN_DISTINFO:
            continue
        src = site / rel
        if src.is_file():
            entries.append((rel, src))
    return entries, outside


class Progress:
    """字节进度：每 ~0.4 秒打一行 "PROG 已写 总量"，给打包器的进度条用（大轮子 torch 要写好几分钟，不能一声不吭）。"""

    def __init__(self, total: int, stream=None):
        self.total = max(int(total), 1)
        self.done = 0
        self._last = 0.0
        self._stream = stream or sys.stdout

    def _emit(self):
        print(f"PROG {min(self.done, self.total)} {self.total}", file=self._stream, flush=True)

    def start(self):
        self._emit()

    def add(self, n: int):
        self.done += n
        now = time.monotonic()
        if now - self._last >= 0.4:
            self._last = now
            self._emit()

    def finish(self):
        self.done = self.total
        self._emit()


def build_wheel(rec, out_dir: str, level: int, gathered=None, progress=None):
    """把一个已装的包还原成 wheel。gathered 是 gather_entries 的结果（给了就不再扫一遍磁盘）；
    progress 是 Progress（每写一块就报一次字节数）。返回 (文件名, 轮子字节, 原始字节, 文件数, outside)。"""
    distinfo = rec["distinfo"]
    stem = distinfo.name[: -len(".dist-info")]
    filename = f"{stem}-{rec['tag']}.whl"
    target = os.path.join(out_dir, filename)
    tmp = target + ".part"
    entries, outside = gathered if gathered is not None else gather_entries(rec)

    record_rows = []
    total = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level, allowZip64=True) as zf:
        for rel, src in entries:
            zi = zipfile.ZipInfo.from_file(src, rel, strict_timestamps=False)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi._compresslevel = level  # noqa: SLF001 —— open('w') 不看 ZipFile 的默认压缩级别
            h, size = hashlib.sha256(), 0
            with open(src, "rb") as fin, zf.open(zi, "w", force_zip64=True) as fout:
                while True:
                    chunk = fin.read(_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    fout.write(chunk)
                    size += len(chunk)
                    if progress is not None:
                        progress.add(len(chunk))
            record_rows.append(f"{rel},{_record_hash(h)},{size}")
            total += size
        record_name = f"{distinfo.name}/RECORD"
        record_rows.append(f"{record_name},,")
        zf.writestr(record_name, "\r\n".join(record_rows) + "\r\n")
    os.replace(tmp, target)
    return filename, os.path.getsize(target), total, len(entries), outside


def cmd_export(args) -> int:
    os.makedirs(args.out, exist_ok=True)
    exclude = {norm(x) for x in (args.exclude or "").split(",") if x.strip()}
    failed = 0
    # 先把要还原的包与文件都扫一遍，得到总字节数——进度条才有分母（torch 一个包就好几 GB）。
    todo = []
    for rec in inspect_dists():
        label = f"{rec['name']} {rec['version']}"
        if rec["norm"] in exclude:
            print(f"SKIP {label}  按要求不带", flush=True)
        elif rec["skip"]:
            print(f"SKIP {label}  {rec['skip']}", flush=True)
        else:
            gathered = gather_entries(rec)
            todo.append((rec, label, gathered))
    progress = Progress(sum(src.stat().st_size for _, _, g in todo for _, src in g[0]))
    progress.start()
    for rec, label, gathered in todo:
        try:
            filename, wheel_bytes, raw_bytes, nfiles, outside = build_wheel(
                rec, args.out, args.level, gathered=gathered, progress=progress
            )
        except Exception as e:  # 一个包还原失败不该拖垮整批 —— 记一笔，跑完统一返回 1
            failed += 1
            print(f"FAIL {label}  {e}", flush=True)
            continue
        note = ""
        # ../Scripts/xxx.exe 这类控制台入口脚本装的时候由安装器按 entry_points.txt 重新生成，不进轮子是对的；
        # 其它落在 site-packages 之外的东西（头文件、数据）才值得提醒。
        odd = [o for o in outside if "/scripts/" not in o.lower()]
        if odd:
            note = f"  （site-packages 之外的 {len(odd)} 个文件没带：{odd[0]} …）"
        print(f"OK   {label}  {filename}  {wheel_bytes / 1048576:.1f} MB{note}", flush=True)
    progress.finish()
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--spec", action="append", default=[], help="一条要求，如 'numpy>=1.24'；可重复")
    p.add_argument("--exclude", default="")
    p.add_argument("--sizes", action="store_true", help="给每个包附上还原后的原始字节数（要 stat 每个文件，较慢）")
    p.set_defaults(fn=cmd_plan)
    e = sub.add_parser("export")
    e.add_argument("--out", required=True)
    e.add_argument("--exclude", default="")
    e.add_argument("--level", type=int, default=6)
    e.set_defaults(fn=cmd_export)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
