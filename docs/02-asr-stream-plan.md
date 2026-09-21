# 02 · 流式本地语音识别组件 `asr-stream`（sherpa-onnx）方案

> 2026-09-21。主仓方案见 `ruyi-workbench-oss/docs/optimization-plan/51-wave-130-streaming-voice.md`（第 130 波）。
> 用户拍板：**新增**流式小模型做「边说边出字」，第二遍改错用已装好的 Qwen3-ASR（`asr-shim`）或云端；校正默认静默替换；
> 第一版只做麦克风这一路。

## 1. 交付什么

`asr-stream/`：一个只在本机监听的小 HTTP 服务，把 sherpa-onnx 的**流式** Zipformer transducer 包成「有会话的 HTTP」：
每 250 ms 收一块 PCM，回当前的临时文字与新收口的句子。CPU 推理、启动即加载、常驻不卸载。

与 `asr-shim` 的关系：并存、互不依赖。`asr-shim` 是整段识别（第二遍、附件、工具），本组件是第一遍。

## 2. 接口（主仓 13b 代理路由逐项对齐）

- `POST /v1/stream/sessions`，体可空或 `{ "hotwords": ["…"] }`（≤ 200 条，每条 ≤ 40 字）→ `200 { "id": "<32 hex>", "sampleRate": 16000 }`。
  超过 4 个活会话 → `429 {"error":{"message":"…","type":"too_many_sessions"}}`。
- `POST /v1/stream/sessions/{id}/audio`，`Content-Type: audio/L16; rate=16000`（也接受 `application/octet-stream`），
  体 = 16 kHz 单声道 PCM16LE，单块 ≤ 1 MB（超 → 413），奇数字节 → 400。
  → `200 { "partial": "…", "finals": [{ "text": "…", "startMs": 0, "endMs": 0 }] }`。`finals` 是自上次调用以来端点检测收口的句子（可空）。
- `POST /v1/stream/sessions/{id}/finish` → 把尾巴当一句收口 → `200 { "finals": [...] }`，会话随即关闭。
- `DELETE /v1/stream/sessions/{id}` → 204。不存在的 id → 404。
- `GET /health` → `200 { "ok": true, "component": "ruyi-asr-stream", "version": "…", "loaded": true, "sessions": n, "model": "…" }`。
  健康探针不做重活（模型在启动时已加载）。
- 错误体一律 `{"error":{"message":"人话","type":"…"}}`。

## 3. 行为

- 端点检测用 sherpa 的三条规则：rule1（一直没说话）静音 2.0 s；rule2（说过话之后）静音 0.8 s；rule3 单句 20 s 硬切。
  可经 `RUYI_ASR_STREAM_RULE2_SEC` 等环境变量调。
- 解码：缺省 `greedy_search`；会话带热词时该会话用 `modified_beam_search` + 热词（sherpa 支持）。
- 会话 30 s 无音频自动关闭；进程内解码串行（两线程）。
- 模型：缺省 `sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20`（int8 编码器 ≈ 174 MB）；`RUYI_ASR_STREAM_MODEL_DIR`
  指向任何 sherpa-onnx 流式 transducer 目录（需含 `encoder`/`decoder`/`joiner` onnx 与 `tokens.txt`）。

## 4. 安全与隐私（与 `asr-shim` 同一套，不放宽）

只绑 `127.0.0.1`；`Host` 不是 `127.0.0.1:<port>`／`localhost:<port>` 一律 403；带 `Origin` 一律 403；不回 CORS 头；
音频只在内存；日志只记元数据（会话数、字节数、时长、耗时、文本长度），**不记文本**；`RUYI_TOOLBOX_PARENT_PID` 看门狗。

## 5. 登记

```json
{ "schema": 1, "id": "asr-stream", "kind": "service", "name": "本地实时语音识别（流式）", "version": "0.1.0",
  "run": { "command": "<venv>\\Scripts\\python.exe", "args": ["-m", "ruyi_asr_stream"], "cwd": "<asr-stream 目录>",
           "env": { "RUYI_ASR_STREAM_MODEL_DIR": "<模型目录>" } },
  "service": { "port": 8791, "portEnv": "RUYI_ASR_STREAM_PORT", "health": "/health", "component": "ruyi-asr-stream" },
  "provides": [ { "type": "asr-stream", "basePath": "/v1", "model": "zipformer-bilingual-zh-en" } ] }
```

`asr-stream` 这个 `provides.type` 已补进 `docs/00-component-registry.md` §2.2。

## 6. 安装

`scripts/install.ps1`（uv venv + `sherpa-onnx` 轮子，PyPI 有 cp312 win_amd64）→ `scripts/download-model.ps1`
（GitHub releases 直连；大陆走 `-Source hf` + `HF_ENDPOINT=https://hf-mirror.com`；执行时核实 ModelScope 有无镜像）→ 登记。
不需要 torch，不需要显卡，装完不到 300 MB。

## 7. 测试

`tests/`：假识别器（可注入）—— 会话生命周期、分块/奇数字节/超限、端点收口、finish 收尾、429、热词入参、Host/Origin 闸、看门狗。
不载真模型、不联网。真机冒烟另有脚本：一段 5 s 合成语音按 250 ms 送，断言 partial 非空且 final 文本正确。

## 8. 131 波补记（2026-09-21，主仓 52 号文）

- **131a**：解码缺省改 `modified_beam_search(4)`（`RUYI_ASR_STREAM_DECODING` 可回 greedy）。评测 hard 档错字 6.78→5.92，每块耗时不变；热词从此不依赖「有没有热词文件」。
- **131c**：同一进程再载一个离线识别器（SenseVoice-small int8，CPU），开 `POST /v1/audio/transcriptions`（OpenAI 形 multipart，只认 WAV）与 `GET /v1/models`；
  登记 `provides` 同时有 `asr-stream` 与 `asr`（`protocol: transcriptions`，模型名 `sensevoice-small`）。`download-model.ps1` 缺省一并下载并登记；`-NoOffline` 跳过。
  离线模型坏了不拖死流式那条路：记一行、照常起、端点回 409。评测：一句 4.5 s 音频 0.25 s，错字率 0.72/0.87/2.74（clean/noisy/hard），接近 Qwen3-ASR-0.6B。
- 不做：换第一遍模型（纯中文 2025 模型对夹英文术语的话更差；Paraformer 12/40 丢尾字）、热词默认开（没用）。
