"""按 `docs/00-component-registry.md` 登记／注销自己。

登记文件是 `~/.ruyi-toolbox/components/asr-shim.json`。**这是本组件在自己安装目录之外唯一
会写的文件**；不碰如意的数据目录 `~/.win-claude-workbench`，也不写注册表、不写开机自启。

两条纪律（约定 §2 第 4 条）：
1. 「登记文件存在＝如意会去执行它」——所以先自检（解释器在、用它真 import 得了依赖、模型文件下全了）
   再写；自检不过就不写，并在 stderr 说清哪一条不过。
2. 原子写：同目录临时文件 + os.replace。半截的 JSON 会让如意启动时看到一个坏文件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import COMPONENT_ID, COMPONENT_NAME_TAG, __version__

SCHEMA = 1
DISPLAY_NAME = "本地语音识别（Qwen3-ASR）"
# 测试用的改道口子（只有测试会设；正式运行一律走用户主目录）。
DIR_ENV = "RUYI_TOOLBOX_COMPONENTS_DIR"
# 登记前用【登记里的那个解释器】真 import 一遍跑起来要的依赖：pyproject 里转写真会 import 的 numpy、transformers，
# 加上 install.ps1 按显卡另装的 torch（不写在 pyproject 里）。soundfile 缺了只是少几种格式，不拦。
IMPORT_CHECK = ("numpy", "transformers", "torch")
IMPORT_CHECK_TIMEOUT_SEC = 180   # 冷盘上第一次 import torch 能要几十秒


def components_dir(env=None) -> Path:
    env = os.environ if env is None else env
    override = str(env.get(DIR_ENV, "")).strip()
    if override:
        return Path(override)
    return Path.home() / ".ruyi-toolbox" / "components"


def registration_path(env=None) -> Path:
    return components_dir(env) / (COMPONENT_ID + ".json")


@dataclass
class CheckResult:
    ok: bool
    problems: list[str]


def self_check(python_exe: str, cwd: str, model_dir: str, models_root: str = "") -> CheckResult:
    problems: list[str] = []
    if not python_exe or not os.path.isabs(python_exe):
        problems.append("解释器路径不是绝对路径：%r" % python_exe)
    elif not os.path.isfile(python_exe):
        problems.append("解释器不存在：%s" % python_exe)

    if not cwd or not os.path.isabs(cwd):
        problems.append("工作目录不是绝对路径：%r" % cwd)
    elif not os.path.isdir(cwd):
        problems.append("工作目录不存在：%s" % cwd)
    elif not os.path.isfile(os.path.join(cwd, "ruyi_asr_shim", "__init__.py")):
        problems.append("工作目录里没有 ruyi_asr_shim 包：%s" % cwd)

    if models_root:
        # auto 模式：models 根目录里至少一份下全的候选；登记会把每份「看着装好」的尺寸都列进清单，所以每份都得真下全
        from .autopick import installed  # noqa: PLC0415

        if not os.path.isabs(models_root):
            problems.append("models 目录不是绝对路径：%r" % models_root)
        elif not os.path.isdir(models_root):
            problems.append("models 目录不存在：%s" % models_root)
        elif not installed(models_root):
            problems.append("models 目录里没有下全的 Qwen3-ASR（要 Qwen3-ASR-0.6B-hf 或 Qwen3-ASR-1.7B-hf，含 config.json 与权重）：%s" % models_root)
        else:
            for c in installed(models_root):
                problems.extend(model_dir_problems(os.path.join(models_root, c.dirname)))
    elif not model_dir:
        problems.append("没有指定模型目录（--model-dir 或 RUYI_ASR_MODEL_DIR）；"
                        "先跑 scripts/download-model.ps1 把模型拉下来")
    else:
        problems.extend(model_dir_problems(model_dir))

    # 依赖最后查（import torch 要好几秒）：解释器本身就不在时没得查
    if python_exe and os.path.isabs(python_exe) and os.path.isfile(python_exe):
        why = import_problem(python_exe, cwd)
        if why:
            problems.append(why)
    return CheckResult(ok=not problems, problems=problems)


def model_dir_problems(model_dir: str) -> list[str]:
    """一份 Qwen3-ASR 模型目录下全了没有（download-model.ps1 的「下完了」判据也调它，同一口径）：
    config.json 在；权重（*.safetensors / *.bin）至少一个、个个非零字节；目录里没有下载器没收尾的 *.incomplete；
    有分片索引（*.index.json）就核对它列的分片个个都在、非零。回空列表＝下全了。"""
    if not os.path.isdir(model_dir):
        return ["模型目录不存在：%s" % model_dir]
    problems: list[str] = []
    names = sorted(os.listdir(model_dir))
    if not os.path.isfile(os.path.join(model_dir, "config.json")):
        problems.append("模型目录里没有 config.json，看着不像一份下全了的模型：%s" % model_dir)
    partial = [n for n in names if n.endswith(".incomplete")]
    if partial:
        problems.append("模型目录里有没下完的文件 %s（下载中断？）：%s —— 重跑 scripts/download-model.ps1" % (partial[0], model_dir))
    weights = [n for n in names if n.endswith((".safetensors", ".bin"))]
    if not weights:
        problems.append("模型目录里没有权重文件（*.safetensors）：%s" % model_dir)
    empty = [n for n in weights if _size(os.path.join(model_dir, n)) <= 0]
    if empty:
        problems.append("权重文件 %s 是 0 字节（下载中断？）：%s —— 重跑 scripts/download-model.ps1" % ("、".join(empty), model_dir))
    for idx in (n for n in names if n.endswith(".index.json")):
        try:
            with open(os.path.join(model_dir, idx), encoding="utf-8") as fh:
                shards = sorted({str(v) for v in json.load(fh)["weight_map"].values()})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            problems.append("分片索引 %s 读不了（下载中断？）：%s" % (idx, model_dir))
            continue
        missing = [n for n in shards if _size(os.path.join(model_dir, n)) <= 0]
        if missing:
            problems.append("分片索引 %s 列的 %d 个分片缺了 %d 个（%s）：%s —— 重跑 scripts/download-model.ps1"
                            % (idx, len(shards), len(missing), "、".join(missing[:3]) + (" …" if len(missing) > 3 else ""), model_dir))
    return problems


def _size(path: str) -> int:
    """文件字节数；不在／不是文件回 -1。"""
    try:
        return os.path.getsize(path) if os.path.isfile(path) else -1
    except OSError:
        return -1


def import_problem(python_exe: str, cwd: str = "") -> str | None:
    """用登记里的那个解释器（如意就用它起服务）在子进程里 import 一遍 IMPORT_CHECK。过了回 None，没过回一句人话。"""
    mods = "、".join(IMPORT_CHECK)
    try:
        r = subprocess.run(
            [python_exe, "-c", "import " + ", ".join(IMPORT_CHECK)],
            cwd=cwd if cwd and os.path.isdir(cwd) else None,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=IMPORT_CHECK_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return "解释器 %s 在 %d 秒内没 import 完依赖（%s）" % (python_exe, IMPORT_CHECK_TIMEOUT_SEC, mods)
    except OSError as exc:
        return "解释器 %s 跑不起来：%s" % (python_exe, exc)
    if r.returncode != 0:
        lines = [ln.strip() for ln in r.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        why = lines[-1][:300] if lines else "退出码 %d" % r.returncode
        return "解释器 %s 里 import 不了依赖（%s）：%s —— 先跑 scripts/install.ps1" % (python_exe, mods, why)
    return None


def build_record(
    *,
    python_exe: str,
    cwd: str,
    model_dir: str,
    model_name: str,
    port: int,
    now: datetime | None = None,
    models_root: str = "",
) -> dict:
    from .autopick import AUTO_MODEL_NAME, catalog, is_auto  # noqa: PLC0415

    env: dict[str, str] = {}
    models: list[dict] = []
    if models_root and is_auto(model_name):
        # 登记只记 models 根目录；auto 挑哪份在加载时决定（autopick.py：缺省最省显存的那份）。
        # 第 133 波：provides.models 把 auto 与每份装好的尺寸都列出来，用户在如意的语音设置里自己挑（1.7B 更准但占 5 GB 显存）；
        # 如意按登记文件画清单，所以下了新尺寸要重跑 download-model.ps1（它会重新登记）。
        env["RUYI_ASR_MODEL"] = "auto"
        env["RUYI_ASR_MODELS_ROOT"] = models_root
        model_name = AUTO_MODEL_NAME
        models = catalog(models_root)
    else:
        if model_dir:
            env["RUYI_ASR_MODEL_DIR"] = model_dir
        if model_name and model_name.lower() != "qwen3-asr-0.6b":
            env["RUYI_ASR_MODEL"] = model_name  # 缺省值不写进去，少一处会过期的事实
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "schema": SCHEMA,
        "id": COMPONENT_ID,
        "kind": "service",
        "name": DISPLAY_NAME,
        "version": __version__,
        "run": {
            "command": os.path.abspath(python_exe),
            "args": ["-m", "ruyi_asr_shim"],
            "cwd": os.path.abspath(cwd),
            "env": env,
        },
        "service": {
            "port": int(port),
            "portEnv": "RUYI_ASR_PORT",
            "health": "/health",
            "component": COMPONENT_NAME_TAG,
            # 第 133 波：如意在用户把语音识别切走（换模型／换服务商／关掉）时 POST 这条路，立刻释放显存
            "unload": "/v1/unload",
        },
        "provides": [
            {
                "type": "asr",
                "basePath": "/v1",
                "model": model_name,
                "protocol": "transcriptions",
                # 可选的多尺寸清单（auto 模式才有）；如意老版本不认这个字段就只用 model
                **({"models": models} if models else {}),
            }
        ],
        "registeredAt": stamp.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (stamp.microsecond // 1000),
    }


def write_record(record: dict, path: Path) -> None:
    """UTF-8 无 BOM、LF、末尾一个换行；同目录临时文件 + replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    data = payload.encode("utf-8")  # 不写 BOM
    fd, tmp = tempfile.mkstemp(prefix=".%s." % COMPONENT_ID, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def register(
    *,
    model_dir: str,
    model_name: str,
    port: int,
    python_exe: str = "",
    cwd: str = "",
    env=None,
    out=None,
    models_root: str = "",
) -> int:
    """写登记文件。回 0 成功、非 0 失败（自检不过时【不写】）。"""
    out = out or sys.stderr
    # run.command 默认就是「现在这个解释器」—— install.ps1 用 venv 的 python 调 register，
    # 于是登记的必然是那个 venv，不需要用户再填一遍路径，也不会填错。
    python_exe = os.path.abspath(python_exe or sys.executable)
    cwd = os.path.abspath(cwd or _package_root())
    model_dir = os.path.abspath(model_dir) if model_dir else ""
    from .autopick import is_auto  # noqa: PLC0415

    models_root = os.path.abspath(models_root) if (models_root and is_auto(model_name)) else ""
    if is_auto(model_name) and not models_root:
        print("登记失败：--model auto 必须同时给 --models-root <models 目录>。", file=out)
        return 3

    check = self_check(python_exe, cwd, model_dir, models_root)
    if not check.ok:
        print("登记失败，自检没过（登记文件存在就意味着如意会去执行它，所以不写）：", file=out)
        for p in check.problems:
            print("  - " + p, file=out)
        return 3

    record = build_record(
        python_exe=python_exe, cwd=cwd, model_dir=model_dir,
        model_name=model_name, port=port, models_root=models_root,
    )
    path = registration_path(env)
    write_record(record, path)
    print("已登记：%s" % path, file=out)
    print("如意下次启动就会自动发现并拉起它；不想要就 python -m ruyi_asr_shim unregister。", file=out)
    return 0


def unregister(env=None, out=None) -> int:
    """删登记文件。文件本来就不在也算成功。"""
    out = out or sys.stderr
    path = registration_path(env)
    try:
        path.unlink()
        print("已注销：%s" % path, file=out)
    except FileNotFoundError:
        print("本来就没有登记文件（%s），无事可做。" % path, file=out)
    except OSError as exc:
        print("删不掉登记文件 %s：%s" % (path, exc), file=out)
        return 4
    return 0


def _package_root() -> str:
    """asr-shim 目录（ruyi_asr_shim 包的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
