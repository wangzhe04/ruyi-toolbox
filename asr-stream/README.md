# asr-stream —— 本地实时语音识别（流式，sherpa-onnx）

把 sherpa-onnx 的**流式** Zipformer 包成一个只在本机监听的小 HTTP 服务，给如意工作台的麦克风**边说边出字**。
装好之后如意会自动发现并接入：输入框的麦克风从「按停顿切段、说完一句等一下出字」变成「说的同时字就在出」。

它管的是**第一遍**（立刻出字）。第二遍（句尾静默改错）由 [`asr-shim`](../asr-shim/)（Qwen3-ASR）或任何你在如意里配的
云端语音识别负责 —— 两个组件并存、互不依赖；只装本组件也能用，只是错字没人改。

- 只绑 `127.0.0.1`；音频只在内存里；日志不记文本。
- **CPU 推理、不用显卡、不用 torch**；模型启动即加载、常驻（几十 MB 内存）。
- 中英双语（缺省模型）；热词可配。

## 三步装好

```powershell
cd <ruyi-toolbox>\asr-stream

# 1) 建环境、装 sherpa-onnx（PyPI 有 Windows 轮子，很快）
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

# 2) 下模型（约 500 MB 的压缩包，用到的四个文件约 200 MB）并向如意登记
powershell -ExecutionPolicy Bypass -File .\scripts\download-model.ps1
#    大陆直连 GitHub 慢的话：
powershell -ExecutionPolicy Bypass -File .\scripts\download-model.ps1 -Source hf

# 3) 重启如意。设置 → 模型服务商 → 「实时识别」会自动选中它
```

需要 [uv](https://docs.astral.sh/uv/)。venv、模型都在本目录内，不进 git。

## 实测（2026-09-21，本机 CPU：AMD Ryzen 笔记本，2 线程）

模型 `sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20`（int8 编码器 182 MB）。样例是 Windows 语音合成造的 16 kHz 单声道 WAV。

| 量的是什么 | 实测 |
| --- | --- |
| 启动到 `/health` 就绪（含加载模型） | **2.1 s** |
| 每块 250 ms 音频的解码耗时 | **16–39 ms** |
| 5.43 s 中文，尽快送 | 端到端 0.56 s（＝ 22 块 × 解码）；逐字正确 |
| 18.9 s 中／英／中三句（各隔 1 s 静音），按真实时间送 | 三句各自在**句尾后约 1 s** 收口（端点规则 rule2 = 0.8 s + 一块粒度） |
| 英文那句 | `PLEASE REMIND ME TO REVIEW THE POOR REQUEST …`（全大写、无标点，「pull」听成「poor」—— 这就是第二遍要改的） |

## 接口（「有会话的 HTTP」，不是 WebSocket）

| 路由 | 说明 |
| --- | --- |
| `GET /health` | `{ok, component:"ruyi-asr-stream", version, loaded, sessions, model}` |
| `POST /v1/stream/sessions` | 开会话 → `{id, sampleRate:16000}`；体可带 `{"hotwords":["…"]}`（≤ 200 条 × 40 字）；超 4 个活会话 → 429 |
| `POST /v1/stream/sessions/{id}/audio` | `Content-Type: audio/L16; rate=16000`，体 = 16 kHz 单声道 PCM16LE，单块 ≤ 1 MB → `{partial, finals:[{text,startMs,endMs}]}` |
| `POST /v1/stream/sessions/{id}/finish` | 冲尾巴、关会话 → `{finals}` |
| `DELETE /v1/stream/sessions/{id}` | 关会话 → 204 |

会话 30 s 没音频自动关闭。`Host` 头不对、带 `Origin` 头一律 403（与 asr-shim 同一套闸）。

## 配置（全部走环境变量或命令行）

| 环境变量 | 缺省 | 说明 |
| --- | --- | --- |
| `RUYI_ASR_STREAM_PORT` | `8791` | 监听端口；如意可能另挑一个经它告诉本服务 |
| `RUYI_ASR_STREAM_MODEL_DIR` | 空（必填） | sherpa-onnx 流式 transducer 模型目录（含 `tokens.txt` 与 encoder/decoder/joiner onnx；encoder、joiner 优先用 int8 版） |
| `RUYI_ASR_STREAM_MODEL` | `zipformer-bilingual-zh-en` | 如意里显示的模型名 |
| `RUYI_ASR_STREAM_THREADS` | `2` | onnxruntime 线程数 |
| `RUYI_ASR_STREAM_RULE1_SEC` / `RULE2_SEC` / `RULE3_SEC` | `2.0` / `0.8` / `20` | 端点规则：一直没说话的静音／说过话之后的静音／单句最长 |
| `RUYI_ASR_STREAM_HOTWORDS_FILE` | 空 | 热词文件（一行一个）；给了就用 modified_beam_search |
| `RUYI_ASR_STREAM_MAX_SESSIONS` / `IDLE_SEC` | `4` / `30` | 并发会话上限／空闲回收 |
| `RUYI_TOOLBOX_PARENT_PID` | 空 | 如意拉起时会给：父进程没了就自己退出 |

子命令：`register` / `unregister` / `doctor`。排障：`scripts\start.ps1` 前台起、`scripts\smoke.py <wav>` 按块送一段音频看 partial／final 与耗时。

## 已知限制

1. 第一遍**没有标点、英文全大写**，专业词（如 pull request）容易听错 —— 都靠第二遍改。只装本组件不装第二遍就是这个样子。
2. 嘈杂环境、方言、数字日期明显不如 Qwen3-ASR。
3. 只支持 sherpa-onnx 的流式 transducer 模型；Paraformer 流式版要改一行构造，先没做。

## 停用与卸载

如意设置页里停用；`.venv\Scripts\python.exe -m ruyi_asr_stream unregister` 撤销登记；删掉 `.venv` 与 `models` 即彻底卸载。
不写注册表、不设开机自启。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

不载真模型、不联网（后端可注入，测试用假的）。
