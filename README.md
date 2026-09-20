# ruyi-toolbox

[如意工作台（Ruyi Workbench）](https://github.com/wangzhe04/ruyi-workbench-oss)的**可选组件仓库**。

主仓有两条红线：服务端零 npm 依赖；离线包不带模型、不带 Python 依赖。凡是「要装一堆东西才能跑、
而且不是人人都需要」的东西住在这里 —— 本地模型 shim、补充 MCP server、小工具。

**装好即被如意自动接入。** 每个组件装完会在 `~/.ruyi-toolbox/components/` 下放一份登记文件；
如意启动时扫这个目录，**是服务就拉起来并接成对应的能力端点，是 MCP 就加进 MCP 服务器清单**。
自动，但不是悄悄：第一次接入会给你一条看得见的提示，设置页里能逐个停用，也能整体关掉自动发现。
卸载就是删掉那份登记文件（组件自带 `unregister`）。约定全文见
[`docs/00-component-registry.md`](docs/00-component-registry.md)。

主仓**不依赖**本仓；本仓组件只通过主仓已有的公开接口接进去。

## 组件索引

| 目录 | 是什么 | 状态 |
| --- | --- | --- |
| [`asr-shim/`](asr-shim/) | 本地语音识别：把 **Qwen3-ASR** 包成 OpenAI 兼容的 `/v1/audio/transcriptions`，音频不出本机 | 可用 |
| [`mcp/`](mcp/) | 以后的补充 MCP server 住这里 | 占位，暂无代码 |

## 文档

- [`docs/00-component-registry.md`](docs/00-component-registry.md) —— 组件怎么向如意登记自己（两仓之间唯一的对接面）
- [`docs/01-asr-shim-plan.md`](docs/01-asr-shim-plan.md) —— asr-shim 的方案与交办单

## 约定

- 每个组件一个顶层目录、各自独立安装、各自一份 README。根 README 只做索引。
- venv、模型权重、样例音频都**不进 git**（见 `.gitignore`）。
- `.ps1` 一律 **UTF-8 with BOM + CRLF**：Windows PowerShell 5.1 读无 BOM 的 UTF-8 会把中文读坏。
- 许可证 Apache-2.0，与主仓一致。
