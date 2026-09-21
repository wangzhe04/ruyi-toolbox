"""向如意登记本组件（登记约定 docs/00-component-registry.md；与 asr-shim 同一模具）。

register：先自检（解释器在、工作目录对、模型目录里有 tokens + encoder/decoder/joiner），过了才原子写登记文件。
自检不过就【绝不】写：登记文件存在 = 如意会去执行它。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import COMPONENT_ID, COMPONENT_NAME_TAG, DEFAULT_MODEL_NAME, __version__

SCHEMA = 1
DISPLAY_NAME = "本地实时语音识别（流式，sherpa-onnx）"
DIR_ENV = "RUYI_TOOLBOX_COMPONENTS_DIR"   # 测试用的改道口子；正式运行一律走用户主目录
DEFAULT_PORT = 8791


def components_dir(env=None) -> Path:
    env = os.environ if env is None else env
    override = str(env.get(DIR_ENV, "")).strip()
    if override:
        return Path(override)
    return Path.home() / ".ruyi-toolbox" / "components"


def registration_path(env=None) -> Path:
    return components_dir(env) / (COMPONENT_ID + ".json")


class CheckResult:
    def __init__(self, problems: list[str]):
        self.problems = problems
        self.ok = not problems


def self_check(python_exe: str, cwd: str, model_dir: str) -> CheckResult:
    problems: list[str] = []
    if not python_exe or not os.path.isabs(python_exe):
        problems.append("解释器不是绝对路径：%r" % python_exe)
    elif not os.path.isfile(python_exe):
        problems.append("解释器不存在：%s" % python_exe)
    if not cwd or not os.path.isabs(cwd):
        problems.append("工作目录不是绝对路径：%r" % cwd)
    elif not os.path.isdir(cwd):
        problems.append("工作目录不存在：%s" % cwd)
    elif not os.path.isfile(os.path.join(cwd, "ruyi_asr_stream", "__init__.py")):
        problems.append("工作目录里没有 ruyi_asr_stream 包：%s" % cwd)
    if not model_dir:
        problems.append("没给模型目录（--model-dir 或 RUYI_ASR_STREAM_MODEL_DIR）")
    elif not os.path.isabs(model_dir):
        problems.append("模型目录不是绝对路径：%r" % model_dir)
    else:
        try:
            from .engine import resolve_model_files  # noqa: PLC0415

            resolve_model_files(model_dir)
        except FileNotFoundError as exc:
            problems.append(str(exc))
    return CheckResult(problems)


def build_record(*, python_exe: str, cwd: str, model_dir: str, model_name: str, port: int) -> dict:
    env: dict[str, str] = {"RUYI_ASR_STREAM_MODEL_DIR": model_dir}
    if model_name and model_name != DEFAULT_MODEL_NAME:
        env["RUYI_ASR_STREAM_MODEL"] = model_name
    return {
        "schema": SCHEMA,
        "id": COMPONENT_ID,
        "kind": "service",
        "name": DISPLAY_NAME,
        "version": __version__,
        "run": {
            "command": os.path.abspath(python_exe),
            "args": ["-m", "ruyi_asr_stream"],
            "cwd": os.path.abspath(cwd),
            "env": env,
        },
        "service": {
            "port": int(port),
            "portEnv": "RUYI_ASR_STREAM_PORT",
            "health": "/health",
            "component": COMPONENT_NAME_TAG,
        },
        "provides": [{"type": "asr-stream", "basePath": "/v1", "model": model_name or DEFAULT_MODEL_NAME}],
        "registeredAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def write_record(record: dict, path: Path) -> None:
    """UTF-8 无 BOM、LF；先写同目录临时文件再 rename（原子）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".%s." % COMPONENT_ID, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register(*, model_dir: str = "", model_name: str = DEFAULT_MODEL_NAME, port: int = DEFAULT_PORT,
             python_exe: str = "", cwd: str = "", env=None, out=None) -> int:
    out = sys.stdout if out is None else out
    python_exe = os.path.abspath(python_exe or sys.executable)
    cwd = os.path.abspath(cwd or _package_root())
    model_dir = os.path.abspath(model_dir) if model_dir else ""
    check = self_check(python_exe, cwd, model_dir)
    if not check.ok:
        print("自检没过，不登记（登记文件存在 = 如意会去执行它）：", file=out)
        for p in check.problems:
            print("  - " + p, file=out)
        return 1
    record = build_record(python_exe=python_exe, cwd=cwd, model_dir=model_dir, model_name=model_name, port=port)
    path = registration_path(env)
    write_record(record, path)
    print("已登记：%s" % path, file=out)
    print("如意下次启动就会自动发现并拉起它；不想要就 python -m ruyi_asr_stream unregister。", file=out)
    return 0


def unregister(env=None, out=None) -> int:
    out = sys.stdout if out is None else out
    path = registration_path(env)
    try:
        path.unlink()
        print("已撤销登记：%s" % path, file=out)
    except FileNotFoundError:
        print("本来就没登记：%s" % path, file=out)
    return 0


def _package_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
