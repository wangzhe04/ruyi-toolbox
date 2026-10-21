"""Apply a Qwen model-only bundle using the registered installation's Python."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def child(root, name):
    path = (root / name).resolve()
    if path == root.resolve() or not path.is_relative_to(root.resolve()):
        raise ValueError("模型路径越界：" + name)
    return path


def verify(root, files):
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != set(files):
        raise ValueError("模型文件清单不一致：" + str(root))
    for name, expected in files.items():
        if digest(child(root, name)) != expected:
            raise ValueError("模型文件校验失败：" + name)


def apply(bundle, record_path):
    manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8-sig"))
    record = json.loads(record_path.read_text(encoding="utf-8-sig"))
    if manifest.get("mode") != "model-update" or record.get("id") != "asr-shim":
        raise ValueError("不是有效的 Qwen 增量包或安装登记")
    cwd = Path(record["run"]["cwd"])
    if not cwd.is_absolute() or not (cwd / "ruyi_asr_shim" / "__init__.py").is_file():
        raise ValueError("原安装目录已失效，请先重新安装并登记 asr-shim")
    sys.path.insert(0, str(cwd))
    from ruyi_asr_shim.autopick import CANDIDATES, catalog, looks_installed
    from ruyi_asr_shim.registry import self_check, write_record

    env = record["run"].setdefault("env", {})
    old_model = env.get("RUYI_ASR_MODEL_DIR")
    root = Path(env.get("RUYI_ASR_MODELS_ROOT") or (str(Path(old_model).parent) if old_model else str(cwd / "models")))
    if not root.is_absolute():
        raise ValueError("登记中的模型目录不是绝对路径")
    root = root.resolve()
    if old_model and Path(old_model).name not in {c.dirname for c in CANDIDATES}:
        raise ValueError("旧安装使用自定义模型目录，无法自动合并；请先用标准 models 目录重新登记")
    providers = [p for p in record["provides"] if p.get("type") == "asr"]
    if not providers:
        raise ValueError("旧登记缺少 ASR 模型信息")
    check = self_check(record["run"]["command"], str(cwd), "", str(root))
    if not check.ok:
        raise ValueError("原安装自检失败：" + "；".join(check.problems))
    components = manifest["components"]
    if len(components) != 1 or components[0]["id"] != "asr-shim" or not components[0]["modelFiles"]:
        raise ValueError("增量包必须包含 Qwen 模型")
    models = components[0]["modelFiles"]
    allowed = {c.dirname for c in CANDIDATES}
    for name, files in models.items():
        if name not in allowed or not files:
            raise ValueError("旧组件不支持此模型，请先升级组件：" + name)
        source = child(bundle / "asr-shim" / "models", name)
        verify(source, files)
        if not looks_installed(str(source)):
            raise ValueError("模型缺少配置或权重：" + name)
    print("找到原安装：", cwd, flush=True)
    print("模型将补充到：", root, flush=True)
    added = []
    try:
        for name, files in models.items():
            dest = child(root, name)
            if dest.exists():
                verify(dest, files)
                print("模型已存在且校验通过：", name, flush=True)
                continue
            stage = Path(tempfile.mkdtemp(prefix=".ruyi-model-", dir=root))
            try:
                print("正在复制模型：", name, flush=True)
                shutil.copytree(bundle / "asr-shim" / "models" / name, stage, dirs_exist_ok=True)
                verify(stage, files)
                stage.rename(dest)
                added.append(dest)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        check = self_check(record["run"]["command"], str(cwd), "", str(root))
        if not check.ok:
            raise ValueError("；".join(check.problems))
        env["RUYI_ASR_MODELS_ROOT"] = str(root)
        env.pop("RUYI_ASR_MODEL_DIR", None)
        env["RUYI_ASR_MODEL"] = "auto"
        for provider in providers:
            provider["model"] = "qwen3-asr-auto"
            provider["models"] = catalog(str(root))
        write_record(record, record_path)
    except Exception:
        for path in added:
            shutil.rmtree(path)
        raise
    print("模型补充完成，登记已更新。重启如意工作台后可选择 1.7B。", flush=True)


if __name__ == "__main__":
    try:
        apply(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    except Exception as exc:
        print("补充模型失败：" + str(exc), file=sys.stderr)
        sys.exit(1)
