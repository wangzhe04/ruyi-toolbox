"""命令行入口：`python -m ruyi_asr_stream [serve|register|unregister|doctor] [选项]`。不带子命令＝ serve。"""

from __future__ import annotations

import argparse
import os
import sys

COMMANDS = ("serve", "register", "unregister", "doctor")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = "serve"
    if argv and argv[0] in COMMANDS:
        cmd = argv[0]
        argv = argv[1:]
    if cmd == "serve":
        from .server import Settings, serve

        return serve(Settings(argv))
    if cmd == "doctor":
        from .server import doctor

        return doctor()
    if cmd == "unregister":
        from .registry import unregister

        return unregister()
    from . import DEFAULT_MODEL_NAME, DEFAULT_OFFLINE_MODEL_NAME
    from .registry import DEFAULT_PORT, register

    ap = argparse.ArgumentParser(prog="python -m ruyi_asr_stream register")
    ap.add_argument("--model-dir", default=os.environ.get("RUYI_ASR_STREAM_MODEL_DIR", "").strip())
    ap.add_argument("--model", default=os.environ.get("RUYI_ASR_STREAM_MODEL", DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME)
    ap.add_argument("--port", type=int, default=int(os.environ.get("RUYI_ASR_STREAM_PORT", "") or DEFAULT_PORT))
    ap.add_argument("--python", default="")
    ap.add_argument("--cwd", default="")
    # 131c：离线整句识别（SenseVoice）目录；给了就同时 provides asr
    ap.add_argument("--offline-model-dir", default=os.environ.get("RUYI_ASR_STREAM_OFFLINE_MODEL_DIR", "").strip())
    ap.add_argument("--offline-model", default=os.environ.get("RUYI_ASR_STREAM_OFFLINE_MODEL", "").strip() or DEFAULT_OFFLINE_MODEL_NAME)
    ns = ap.parse_args(argv)
    return register(model_dir=ns.model_dir, model_name=ns.model, port=ns.port, python_exe=ns.python, cwd=ns.cwd,
                    offline_model_dir=ns.offline_model_dir, offline_model_name=ns.offline_model)


if __name__ == "__main__":
    raise SystemExit(main())
