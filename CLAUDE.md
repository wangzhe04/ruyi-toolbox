# CLAUDE.md —— 给 Claude Code(本机与云端)的项目说明

本仓是如意工作台([ruyi-workbench-oss](https://github.com/wangzhe04/ruyi-workbench-oss))的**可选组件仓**。
主仓不依赖本仓,两仓唯一的对接面是登记约定 [`docs/00-component-registry.md`](docs/00-component-registry.md)。
改登记文件的字段或语义前先读它,并同步主仓 `04f-toolbox-services.js` 与 `toolbox-discovery.e2e.js`。

## 约定(见 README「约定」)

- 每个组件一个顶层目录、各自安装、各自一份 README。
- venv、模型权重、样例音频不进 git。
- `.ps1` 一律 **UTF-8 with BOM + CRLF**(Windows PowerShell 5.1 读无 BOM 的 UTF-8 会把中文读坏)。
- Windows 是主战场:进程存活用内核答案(`OpenProcess`),不解析 `tasklist`;非 Windows 分支用 `os.kill(pid, 0)` 加 `/proc` 僵尸判定。

## 跑测试

各组件目录下(测试用假后端,不需要模型、torch 或 sherpa-onnx):

```bash
cd asr-shim   && python -m pytest -q     # 或 python -m unittest discover -s tests -t .
cd asr-stream && python -m pytest -q
cd evaluation/document-parser && python -m pytest -q
cd tools && python -m pytest -q          # .ps1 相关用例在非 Windows 上跳过
ruff check --select F .                  # 未配置正式 linter,pyflakes 级检查
```

## 在云端(Linux 容器)开发

`.claude/hooks/session-start.sh` 在云端会话启动时把 numpy、soundfile、pytest、ruff 装进
`~/.cache/ruyi-toolbox-venv`,并把它放上 `PATH`。2026-09 实测上面四组测试在 Linux 上全绿
(少数 Windows 专属用例跳过)。GPU 推理、ROCm/CUDA 验证、`.ps1` 打包器只能在 Windows 真机上验。
