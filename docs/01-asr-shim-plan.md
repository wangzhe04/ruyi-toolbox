# 01 · 本地语音识别 shim（Qwen3-ASR）方案与交办单

> 2026-09-21。出方案：Fable（主会话）；执行：Opus。用户拍板：新开 `ruyi-toolbox` 作为如意工作台（Ruyi Workbench）的
> 补充 MCP／工具仓库，把主仓 26 号文「114d · 本地 shim」放到这里，目标本地模型 **Qwen3-ASR**。

## 1. 这个仓库是什么

主仓 `ruyi-workbench-oss` 有两条红线：服务端零 npm 依赖；离线包不带模型、不带 Python 依赖。凡是「要装一堆东西才能跑、
而且不是人人都需要」的可选组件住这里：本地模型 shim、补充 MCP server、小工具。每个组件一个顶层目录、各自独立安装、
各自一份 README；根 README 只做索引。主仓不依赖本仓，本仓组件通过主仓【已有的公开接口】接进去（本件＝一个普通的
OpenAI 兼容服务商地址）。

## 2. 要交付的东西：`asr-shim/`

一个只在本机监听的小 HTTP 服务，把 Qwen3-ASR 包成 OpenAI 兼容的转写接口。用户在如意里把它当成一个普通服务商加进去，
输入框麦克风、音频附件、`audio_transcribe` 工具三处就都走本地模型，音频不出本机。

### 2.1 接口（与主仓实现逐项对齐，已核实）

主仓出站代码在 `ruyi-workbench/app/src/05-claude-engine.js` 的 `transcribeAudioViaProvider`。缺省协议是：

- `POST {baseUrl}/audio/transcriptions`，`multipart/form-data`，字段：`model`、`response_format=json`、可选 `language`、
  可选 `prompt`、`file`（带 filename 与 content-type）。`baseUrl` 不带 `/v1` 时主仓会补上，所以 shim 的路径是
  **`/v1/audio/transcriptions`**。
- 成功回体必须是 JSON 且含字符串字段 `text`；可带 `language`；可带 `usage`（没有也行，主仓会估算）。
- 非 2xx 时主仓把回体前 1000 字放进错误信息给用户看 —— 所以错误体要是一句人话 JSON：`{"error":{"message":"…","type":"…"}}`。
- 主仓超时 120 秒、上传上限 25 MB。shim 同样按 25 MB 拒收（413）。
- 如意的麦克风现在【按停顿切段】，每段 2–30 秒的 16 kHz 单声道 16 bit WAV，一段一发、同一次录音的各段【串行】到达。
  音频附件与工具那两路发的是用户的原文件（mp3／m4a／webm／ogg／flac 都可能）。

另需：`GET /health`（进程活着即 200，回 `{ok, model, loaded, device, idleUnloadSec}`，**不得**因此触发加载模型）、
`GET /v1/models`（回一条所配模型 id，主仓「测试连接」与模型发现会打它）。

不做：`/v1/chat/completions` 的 `input_audio` 形（主仓的「对话型」协议）。本 shim 用「通用型」就够，少一条路少一处错。

### 2.2 安全与隐私边界（来自 26 号文 §4，不许放宽）

1. **只绑 `127.0.0.1`**。不提供改绑 `0.0.0.0` 的开关。
2. 无鉴权但仅本机可达，所以要防浏览器跨站打本机端口：`Host` 头不是 `127.0.0.1:<port>`／`localhost:<port>` 一律 403
   （防 DNS rebinding）；带 `Origin` 头的请求一律 403（如意服务端用 Node fetch 出站，不带 Origin）；不回任何 CORS 头。
3. **音频不落盘**：优先内存解码；不得不落临时文件时放系统临时目录、`finally` 里删除，启动时清理上次残留。
4. 日志只记元数据（时间、字节数、时长、耗时、语言、文本长度、错误类型），**不记转写文本、不记文件名**。
5. 端口经 `RUYI_ASR_PORT`（缺省 `8790`），模型目录经 `RUYI_ASR_MODEL_DIR`；其余可配项也只走环境变量／命令行，
   不读写如意的数据目录 `~/.win-claude-workbench`。

### 2.3 运行形态

- 模型**懒加载**（第一发转写请求才加载）＋**空闲卸载**（缺省 600 秒无请求就释放显存：删引用、`gc.collect()`、
  `torch.cuda.empty_cache()`）。`RUYI_ASR_IDLE_UNLOAD_SEC=0` 表示常驻。
- 推理**串行**（一把锁）。0.6B 在这台机器上一段几秒的音频应当远快于实时；并发没有收益只有显存风险。
- 设备自动选：有 CUDA 用 CUDA（bfloat16），否则 CPU（float32）并在启动日志里说清楚「现在是 CPU，会慢」。
- 默认模型 `Qwen/Qwen3-ASR-0.6B`；`RUYI_ASR_MODEL` 可换 `Qwen/Qwen3-ASR-1.7B`。`/v1/models` 与回体里的 model 用
  如意侧要填的那个名字：`qwen3-asr-0.6b`／`qwen3-asr-1.7b`（请求里的 `model` 字段只记录、不拒收 —— 如意填什么都能转）。

### 2.4 目标机器（已查实）

Windows 11；**NVIDIA RTX 5080 Laptop 16 GB（Blackwell，sm_120 —— PyTorch 必须是带 CUDA 12.8 的构建，
老的 cu121／cu124 轮子在这块卡上跑不起来）**；系统 Python 3.13.12；有 `uv 0.12`；**没有 ffmpeg**；E 盘余 357 GB；
用户在中国大陆，直连 HuggingFace 可能不通 —— 模型下载要给 ModelScope 一条路（缺省）和 HF／`HF_ENDPOINT` 镜像一条路。

### 2.5 推理后端 —— 必须先查证，不许凭记忆

我（出方案的人）的印象，**未核实**：Qwen 官方有 `qwen-asr` 这个 PyPI 包（仓库 `QwenLM/Qwen3-ASR`），大致用法是
`Qwen3ASRModel.from_pretrained(path, dtype=…, device_map=…)` 然后 `.transcribe(audio=…, language=None)` 取
`results[0].text`／`.language`，另有 vLLM 后端。**动手前先读官方 README／模型卡把真实 API、依赖版本、支持的 Python 版本、
输入格式（路径／ndarray／采样率要求）钉下来**，写进 `asr-shim/docs/backend-notes.md`（附来源链接与查证日期）。
印象与事实不符以事实为准。只用 transformers 后端，不上 vLLM（Windows 上不值得）。

Python 版本：官方依赖若不支持 3.13，就用 `uv venv --python 3.12`（uv 会自己下解释器），不要动系统 Python。

非 WAV 格式的解码：机器上没有 ffmpeg。先看官方包自己怎么读音频（多半是 librosa／soundfile）；WAV／FLAC／OGG 走
soundfile 必须可用；mp3／m4a／webm 能解就解，解不了回 415 加一句人话「这种格式需要装 ffmpeg，或先转成 wav」，
并写进 README 的已知限制。**麦克风那一路（WAV）是必保的主路径。**

### 2.6 代码形状

```
ruyi-toolbox/
  README.md                 # 仓库是什么＋组件索引（中文为主）
  LICENSE                   # Apache-2.0，与主仓一致
  .gitignore  .gitattributes（*.py *.ps1 *.md eol=lf 除 ps1 外；ps1 用 CRLF＋UTF-8 BOM，见下）
  docs/01-asr-shim-plan.md  # 本文
  asr-shim/
    README.md               # 安装、启动、接进如意的三步、排障、实测数字、已知限制
    pyproject.toml          # 依赖（torch 不写死在这里，见安装脚本）
    ruyi_asr_shim/
      __main__.py           # python -m ruyi_asr_shim
      server.py             # http.server（ThreadingHTTPServer）、路由、Host/Origin 闸、25MB 闸、错误信封
      multipart.py          # multipart/form-data 解析（Python 3.13 已删 cgi 模块 —— 自己写或用 email 包，要有单测）
      engine.py             # 引擎接口＋Qwen3 实现：懒加载、串行锁、空闲卸载；引擎可注入（测试用假引擎）
      audio.py              # 字节 → 模型要的输入；格式嗅探；415
    scripts/
      install.ps1           # 建 venv、装对的 torch（cu128）、装其余依赖、自检 torch.cuda.is_available()
      download-model.ps1    # ModelScope 缺省，-Source hf 走 HuggingFace（尊重 HF_ENDPOINT）
      start.ps1             # 起服务（前台）；打印如意里要填的地址
    tests/                  # 不加载真模型：假引擎＋真 HTTP 往返
    docs/backend-notes.md
  mcp/README.md             # 占位：以后的补充 MCP server 住这里（现在不放任何代码）
```

HTTP 层用标准库 `http.server`（26 号文原定；依赖越少，用户装起来越不容易坏）。PowerShell 脚本注意主仓踩过的坑：
**Windows PowerShell 5.1 读无 BOM 的 UTF-8 会把中文读坏 —— .ps1 要么纯 ASCII，要么 UTF-8 带 BOM**；5.1 没有
`&&`／三元／`??`。

### 2.7 测试与验收（做完要能逐条打勾）

不加载真模型的自动化（`python -m pytest` 或 `python -m unittest`，要能在没有 GPU 的机器上全绿）：
- multipart：正常件、中文文件名、缺 `file`、边界串出现在内容里、超 25 MB（413）、非 multipart（400）。
- 路由：`/health` 不触发加载；`/v1/models`；未知路径 404；方法不对 405。
- 安全闸：错 Host 403；带 Origin 403；监听地址断言就是 127.0.0.1。
- 引擎生命周期（假引擎）：懒加载只加载一次；并发两发请求串行执行；空闲到点卸载、下一发重新加载；
  推理抛异常 → 500 人话信封且锁被释放。
- 隐私：一趟请求之后临时目录里没有残留；日志里没有转写文本。

真机冒烟（本机有 GPU，**必须真跑**，结果写进 README「实测」一节）：
- `install.ps1` 从零装通；`download-model.ps1` 把 0.6B 拉下来；`start.ps1` 起服务。
- 用一段中文、一段英文 WAV 各转一次，记录：首发（含加载）耗时、热态耗时、显存占用、文本是否正确。
  中文样例可用 `C:\Users\87179\AppData\Local\Temp\claude\E--Claude-ruyi-workbench-oss\4a963f73-3b58-466d-aa2d-55f38c48a5d4\scratchpad\probe.wav`
  （Windows 语音合成的「你好，今天下午三点开会，请帮我记一下。」16 kHz 单声道）。**样例音频不要提交进仓库**，
  提交一个生成样例的脚本即可。
- 用 `curl`／Python 按 §2.1 的字段形状打一发真 multipart，确认回体能被主仓那段解析代码接受（有字符串 `text`）。
- 空闲卸载真的释放显存（`nvidia-smi` 前后对比，缩短 `RUYI_ASR_IDLE_UNLOAD_SEC` 来测）。

与如意的真联调由主会话在你交付后做（隔离实例，不碰用户数据），你不用做。

## 3. 纪律（硬性）

- **只在 `E:\Claude\ruyi-toolbox` 里干活。** 不改 `E:\Claude\ruyi-workbench-oss` 的任何文件（可以读）；不碰用户正在跑的
  如意（127.0.0.1:8765）；不读写 `~/.win-claude-workbench`；不动系统 Python 的全局包；不装系统级软件（ffmpeg 等）。
- venv、模型、缓存都放在仓库目录内且被 `.gitignore` 挡住（`.venv/`、`models/`），**绝不提交模型权重与样例音频**。
- 如实汇报：跑不通的写跑不通和原因，不许写成通过；没真跑的不许写「实测」。数字只写量到的。
- 提交：小步提交，信息用中文说清「做了什么、为什么」，结尾带
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`。分支 `main`，远端 `origin` 已配好（私有仓
  `wangzhe04/ruyi-toolbox`），**全部做完、测试全绿之后自己 `git push -u origin main`**。
- 下载很大（torch 约 3 GB、模型约 1–2 GB）：长命令用后台运行＋轮询，别让单条命令卡超时。

## 4. 回传（你最后一条消息要包含）

1. 每条验收项的结果（过／没过／没做＋原因）。2. 官方后端查证到的事实与我 §2.5 印象的出入。3. 实测数字。
4. 提交清单（hash＋一句话）与是否已 push。5. 用户接进如意要做的确切步骤（服务商怎么填、模型名填什么）。
6. 你发现的、本文没想到的风险或建议。

## 5. 明确不在本件范围

- 主仓一侧的 `localCommand` 自动拉起／随主进程回收／`doctor` 检查项（26 号文 114d 的后半）—— 那是主仓的活，另开一波。
  本件交付的是「用户手动起一个本地服务、在如意里当普通服务商用」。
- 真流式（边录边传的 WebSocket）。如意已经用「按停顿切段」做到边说边出字，shim 只需要把每一段转得快。
- 把主仓现有的 `mcp/ai-computer-control` 搬过来。

## 6. 追加（2026-09-21，用户新要求）：开箱即用 —— 按通用约定登记自己

用户要求：如意启动时自动发现 toolbox 里的组件，是服务就自动拉起并配置好，是 MCP 就自动配上。
两个仓库之间的对接面写在 **`docs/00-component-registry.md`**（先读它，那是事实源）。如意那一半由主会话在主仓做；
**shim 按那份约定登记自己、守服务类组件的行为，并入本件范围**，原 §5 第一条相应作废。

shim 要做的：

1. 子命令 `python -m ruyi_asr_shim register`／`unregister`。登记文件是 `~/.ruyi-toolbox/components/asr-shim.json`，
   `kind: "service"`，`service.component = "ruyi-asr-shim"`，`service.portEnv = "RUYI_ASR_PORT"`，`service.health = "/health"`，
   `provides = [{ "type": "asr", "basePath": "/v1", "model": "qwen3-asr-0.6b"（按实际所配模型）, "protocol": "transcriptions" }]`，
   `run.command`＝venv 里 python 的绝对路径、`run.args = ["-m","ruyi_asr_shim"]`、`run.cwd`＝asr-shim 目录、
   `run.env` 里放 `RUYI_ASR_MODEL_DIR`（以及非缺省的 `RUYI_ASR_MODEL`）。`register` 先自检（本包可 import、模型目录存在）再原子写。
   `install.ps1`／`download-model.ps1` 走完之后调 `register`；README 写清卸载要先 `unregister`。
2. `/health` 回体加 `"component": "ruyi-asr-shim"` 与 `"version"`。
3. 认 `RUYI_TOOLBOX_PARENT_PID`：父进程看门狗（约定 §2.2）。
4. **空转要轻**：进程起来、没人说话时不许 import torch／transformers。README 写上实测的空转常驻内存。
5. 端口被占立刻非零退出＋stderr 一句人话；不弹窗、不读 stdin；日志走 stderr 且只有元数据。
6. `mcp/README.md` 占位文里指向 `docs/00-component-registry.md` 的 §2.3，根 README 也要提一句「装好即被如意自动接入」。

验收追加：
- `register`／`unregister` 单测：原子写、自检不过不写、字段齐全、路径为绝对路径、输出能被 `json.loads` 读回且无 BOM。
- 看门狗单测：起一个短命子进程当「父」，它退出后 shim 在 10 秒内自己退出。
- 真机：安装脚本跑完登记文件就位；**只用登记文件里的 `run`（command＋args＋cwd＋env，外加 `RUYI_ASR_PORT`）就能把服务起来**
  —— 这正是如意会做的事；量空转常驻内存；杀掉「父」之后 shim 自退、`nvidia-smi` 显存归还。

## 7. 追加（2026-09-21，用户新要求）：显卡加速不只针对英伟达

用户原话：「shim 本身最好也要能兼容 AMD 的显卡加速，不要只针对英伟达卡加速」。

- 设备选择是 engine 里可单测的一层：`RUYI_ASR_DEVICE=auto|cuda|directml|cpu`（名字以查证结果为准），缺省 auto；
  顺序为 torch.cuda 可用（英伟达与 ROCm 构建都走这条）→ 其它已安装且可用的加速后端 → CPU；**任何一档初始化失败自动降到下一档**，
  stderr 留一句人话，不能因为显卡后端坏了整个服务起不来。`/health` 的 `device` 如实反映实际用上的那一档；dtype 随后端选。
- Windows 上 AMD 走哪条路（官方 Windows 版 ROCm／PyTorch，还是 torch-directml）**必须先查官方当前文档**，结论与来源写进
  `asr-shim/docs/backend-notes.md` 的「AMD／非英伟达加速」一节。`install.ps1 -Gpu auto|nvidia|amd|cpu`。
- 验证状态如实标注：开发机只有英伟达卡，**AMD 路径未经真机验证**；选择与降级逻辑用假后端单测覆盖。

## 8. 与如意的真联调记录（2026-09-21，主会话独立验证）

隔离的如意实例（源码树、临时数据目录、端口 8791、`RUYI_TOOLBOX_HOME` 指向真实登记目录），不碰用户正在跑的那一个：

- 启动后自己发现登记 → 按 `run` 拉起 shim → 健康 → 状态 `running`、`owned:true`、端口 8790。
- 自动生成服务商 `toolbox-asr-shim`（`http://127.0.0.1:8790/v1`、模型带语音识别标记）；该实例没配过语音识别，被自动选中，`seen` 记下 `asr-shim`。
- 经如意的 `/api/audio/transcribe` 转中文样例：**逐字正确**。首发 7.0 s（含加载模型），热态 0.62–0.64 s。`/health` 如实显示
  `cuda (NVIDIA GeForce RTX 5080 Laptop GPU, bfloat16)`；探活不触发加载（首发前 `loaded:false`）。
- 杀掉 shim 整棵进程树 → 下一次转写由如意就地重新拉起并成功（6.6 s，含重新加载）。
- 只强杀如意本体（不杀树，模拟崩溃）→ shim 约 0.5 s 内自行退出，显存 3170 → 1365 MiB 归还，8790 端口无残留。
- shim 单测 125 条在主会话这边独立复跑全过。

未验证：AMD 路径（无硬件）；真人声音下的表现（样例是合成语音）。
