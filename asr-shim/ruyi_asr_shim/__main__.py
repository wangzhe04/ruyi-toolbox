"""命令行入口：`python -m ruyi_asr_shim [serve|register|unregister|doctor] [选项]`

不带子命令＝ `serve`（如意的登记文件里 args 就是 `["-m","ruyi_asr_shim"]`，走这一条）。
不读 stdin、不弹窗口；日志走 stderr。
"""

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

    # register
    from .registry import register
    from .server import DEFAULT_PORT, _env_int

    ap = argparse.ArgumentParser(prog="python -m ruyi_asr_shim register")
    ap.add_argument("--model-dir", default=os.environ.get("RUYI_ASR_MODEL_DIR", "").strip(),
                    help="模型目录的绝对路径（缺省读 RUYI_ASR_MODEL_DIR）")
    ap.add_argument("--model", default=os.environ.get("RUYI_ASR_MODEL", "qwen3-asr-0.6b").strip(),
                    help="如意里要填的模型名，如 qwen3-asr-0.6b")
    ap.add_argument("--port", type=int, default=_env_int(os.environ, "RUYI_ASR_PORT", DEFAULT_PORT),
                    help="首选端口；如意探到被占会另挑一个并经 RUYI_ASR_PORT 告知")
    ap.add_argument("--python", default="", help="登记哪个解释器（缺省＝现在这个）")
    ap.add_argument("--cwd", default="", help="登记的工作目录（缺省＝asr-shim 目录）")
    ap.add_argument("--models-root", default=os.environ.get("RUYI_ASR_MODELS_ROOT", "").strip(),
                    help="配合 --model auto：models 目录，里面有几份就按显存挑最大能装下的")
    ns = ap.parse_args(argv)

    return register(
        model_dir=ns.model_dir,
        model_name=ns.model or "qwen3-asr-0.6b",
        port=ns.port,
        python_exe=ns.python,
        cwd=ns.cwd,
        models_root=ns.models_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
