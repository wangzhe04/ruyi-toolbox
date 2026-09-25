#!/bin/bash
# Claude Code 云端会话(Linux 容器)启动钩子:装好跑单元测试所需的最小依赖。
# 不装模型、不装 torch/transformers/sherpa-onnx —— 各组件的测试都用假后端,不需要它们。
# 依赖装进缓存目录下的独立 venv(容器快照会缓存它),幂等、非交互;本机(Windows)开发不触发。
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

venv="$HOME/.cache/ruyi-toolbox-venv"
if [ ! -x "$venv/bin/python" ]; then
  python3 -m venv "$venv"
fi
"$venv/bin/python" -m pip install --quiet --disable-pip-version-check \
  "numpy>=1.24" "soundfile>=0.12" pytest ruff

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export VIRTUAL_ENV=\"$venv\"" >> "$CLAUDE_ENV_FILE"
  echo "export PATH=\"$venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
echo "session-start: $("$venv/bin/python" --version) venv 就绪($venv)"
