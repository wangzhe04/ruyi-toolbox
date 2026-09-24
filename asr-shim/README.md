# asr-shim —— 本地语音识别（Qwen3-ASR）

把 **Qwen3-ASR** 包成一个只在本机监听的 OpenAI 兼容转写服务
（`POST /v1/audio/transcriptions`，Whisper 形）。装好之后**如意工作台会自动发现并接入它**，
输入框的麦克风、音频附件、`audio_transcribe` 工具三处就都走本地模型，**音频不出本机、不落盘**。

- 只绑 `127.0.0.1`，没有改绑 `0.0.0.0` 的开关。
- 模型**懒加载**（第一次转写才加载）＋**空闲卸载**（缺省 600 秒无请求就把显存还回去）。
- 日志只记元数据 —— 不记转写文本，不记文件名。

---

## 三步装好

```powershell
cd E:\Claude\ruyi-toolbox\asr-shim

# 1) 建环境、装 PyTorch（按显卡挑构建）与依赖，最后自检
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

# 2) 下模型（缺省走魔搭 ModelScope，大陆快）并【向如意登记本组件】
powershell -ExecutionPolicy Bypass -File .\scripts\download-model.ps1

# 3) 重启如意 —— 它会自己发现、自己拉起、自己配好语音识别
```

第 3 步之后如意会给你一条「发现并接入了 本地语音识别（Qwen3-ASR）」的提示。
**你不需要手动填服务商地址。** 想自己先看看服务跑不跑得起来：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
# 起来之后：curl http://127.0.0.1:8790/health
```

需要 [uv](https://docs.astral.sh/uv/)（没有的话 `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`）。
不需要 ffmpeg，不动系统 Python。venv、模型、样例音频全在本目录内，都不进 git。

### 如果你想手动接（不用自动发现）

在如意的「服务商」里加一条 OpenAI 兼容服务商：

| 填什么 | 填成 |
| --- | --- |
| 地址 baseUrl | `http://127.0.0.1:8790/v1`（不带 `/v1` 也行，如意会自己补） |
| API Key | 留空 |
| 模型 | `qwen3-asr-0.6b`（auto 登记时可选 `qwen3-asr-auto` / `qwen3-asr-0.6b` / `qwen3-asr-1.7b`） |
| 接口类型 / 协议 | **通用型（transcriptions）**，不要选「对话型 chat-audio」 |

然后在语音识别设置里选这条服务商 + 这个模型。

---

## 实测（2026-09-21，英伟达卡真跑）

AMD 卡的实测在下面「显卡加速 → AMD 卡」一节。

机器：Windows 11、**RTX 5080 Laptop 16 GB**（Blackwell sm_120）、Python 3.12.11、
`torch 2.11.0+cu128`、`transformers 5.17.0`、`soundfile 0.14.0`（libsndfile 1.2.2）。
模型 `Qwen/Qwen3-ASR-0.6B-hf`，磁盘 1.5 GB（`model.safetensors` 1,564,928,088 字节）。
样例音频是 Windows 自带语音合成造的 16 kHz 单声道 WAV（`scripts\make-sample.ps1`）。

### 空转（进程起来了，还没人说话）

| 量的是什么 | 实测 |
| --- | --- |
| 常驻内存（working set） | **35.1 MB**，外加 `.venv\Scripts\python.exe` 那个 5.1 MB 的启动壳 ≈ **40 MB** |
| 是否加载了 torch | **没有** —— 进程里一个 `torch*.dll` / `cudnn` / `cublas` 都没有，总共只加载了 53 个模块 |
| 显存占用 | **0** —— 模型没加载，不碰显卡 |
| 健康探针 | `GET /health` 回 200，**不触发加载** |

（`PrivateMemorySize64` 会显示 760 MB 左右，那是虚拟提交量不是常驻量，别被它吓到。）

### 转写耗时

| 场景 | 音频时长 | 端到端 |
| --- | --- | --- |
| 第一发（含加载），刚装完、文件缓存是冷的 | 5.43 s | **15.6 s**（其中模型加载 14.5 s） |
| 第一发（含加载），文件缓存热 | 5.43 s | **5.7 s**（其中模型加载 4.8 s） |
| 空闲卸载之后重新加载的第一发 | 5.43 s | **2.6 s**（其中模型加载 2.0 s） |
| 热态 · 中文 | 5.43 s | **0.45 – 0.47 s** |
| 热态 · 英文 | 6.03 s | **0.60 – 0.62 s** |
| 热态 · 中文 + 指定 `language=zh` + 热词 | 5.43 s | **0.36 – 0.38 s** |
| 热态 · 长音频（自动切成 2 段） | 39.0 s | **3.45 – 4.48 s** |

也就是热态下大约 **10–16 倍实时**。如意麦克风那一路是 2–30 秒一段、一段一发，
所以一段话说完到出字基本是半秒级 —— 前提是模型已经加载好了。

### 显存

| 时刻 | `nvidia-smi` 全机 | 本进程的部分 |
| --- | --- | --- |
| 模型没加载 | 1.35 GB（这是别的程序占的，本底） | 0 |
| 模型加载后 | 3.16 GB | **≈ 1.8 GB** |
| torch 自己报的 | — | allocated 1495 MB，峰值 1561 MB，reserved 1640 MB |

**空闲卸载实测**（把 `RUYI_ASR_IDLE_UNLOAD_SEC` 调到 45 秒来测）：
最后一发请求之后第 45–50 秒，`/health` 的 `loaded` 变回 `false`，
`nvidia-smi` 从 **3211 MiB 掉到 1591 MiB —— 还回去约 1.62 GB**。
剩下约 240 MB 是 CUDA 上下文本身，那要到进程退出才还，这是 PyTorch 的常态，不是泄漏。
卸载之后下一发请求会自动重新加载（实测 2.6 s）。

### 准确度（就两条样例，不是评测）

- 中文「你好，今天下午三点开会，请帮我记一下。」—— **逐字正确**。
- 英文「Please remind me to review the **pull** request before the meeting at three o'clock.」
  —— 转成了「…review the **poll** request…」。合成语音的口音问题，不是配置问题；
  真人说话或者用 `prompt` 传热词（`prompt=pull request`）能改善。
- 39 秒长音频切成 2 段之后，**切口处吞了一个字**：「另外提醒一下」→「另外提。请一下」。
  这是下面「已知限制」第一条。

### 其它跑过的

| 验的是什么 | 结果 |
| --- | --- |
| 只用登记文件里的 `run`（command＋args＋cwd＋env）＋ `RUYI_ASR_PORT` 拉起（＝如意的做法） | 起得来，健康探针 **0.53 s** 通过，`component` 对得上 |
| 杀掉「父进程」之后看门狗自退（模型没加载时） | **3.31 s** 后自己退出，退出码 0 |
| 杀掉「父进程」之后看门狗自退（**模型正占着 1.8 GB 显存时**） | **4.01 s** 后退出，退出码 0；3 秒后 `nvidia-smi` 从 3166 MiB 回到 1360 MiB，**归还 1806 MiB**，正好回到本底 —— 不留占显存的孤儿 |
| 端口被占 | **立刻退出，退出码 2**，stderr 一句人话告诉你怎么换端口 |
| 日志里有没有转写文本／文件名 | **没有**。只有 `bytes= audio_s= dur_ms= lang= text_len=` 这些元数据 |
| 临时目录残留 | **没有**。音频全程在内存里解码，根本不落盘 |

---

## 配置（全部走环境变量或命令行，不读如意的数据目录）

| 环境变量 | 缺省 | 说明 |
| --- | --- | --- |
| `RUYI_ASR_PORT` | `8790` | 监听端口。**如意可能会另挑一个端口经这个变量告诉本服务，以它为准。** |
| `RUYI_ASR_MODEL_DIR` | 空 | 本地模型目录。不填就尝试联网下载（大陆可能很慢）。 |
| `RUYI_ASR_MODEL` | `qwen3-asr-0.6b` | 也可 `qwen3-asr-1.7b`，或 **`auto`**（见下一行）。这个名字就是如意里要填的模型名（auto 时显示为 `qwen3-asr-auto`）。 |
| `RUYI_ASR_MODELS_ROOT` | 空 | 配合 `RUYI_ASR_MODEL=auto`：`models` 目录。`download-model.ps1` 缺省就这样登记，并把目录里每份装好的尺寸（0.6B／1.7B）都列进登记文件的 `provides[0].models`，如意的语音设置里就能逐份选：**auto 缺省用最省显存的那份（0.6B）**，要更准的在如意里选 `qwen3-asr-1.7b`（约 5 GB 显存）。转写请求里的 `model` 字段点名哪份就加载哪份（换尺寸先卸旧的再载新的，显存里最多一份）。`/health` 的 `resolvedModel` 报实际加载的那份，`models` 报可选清单。下了新尺寸要重跑 `download-model.ps1`（它会重新登记）。 |
| `RUYI_ASR_AUTO_PREFER` | `small` | auto 挑哪份：`small` 最省显存的；`large` 按空闲显存挑最大能装下的（1.7B 要约 4.8 GB 空闲，0.6B 约 2.2 GB；没显卡挑最小的）。 |
| `RUYI_ASR_IDLE_UNLOAD_SEC` | `600` | 空闲多少秒卸载模型。`0` = 常驻不卸。另外 `POST /v1/unload` 立刻卸载（如意在用户把语音识别切走时会打它）。 |
| `RUYI_ASR_DEVICE` | `auto` | `auto` / `cuda` / `amd`(=`rocm`) / `xpu` / `mps` / `directml` / `cpu`。见下。 |
| `RUYI_ASR_DTYPE` | `auto` | `auto` / `bfloat16` / `float16` / `float32`。 |
| `RUYI_ASR_LOG_LEVEL` | `INFO` | |
| `RUYI_TOOLBOX_PARENT_PID` | 空 | 如意拉起时会给。给了就每 3 秒看一眼父进程还在不在，不在了自己干净退出。 |

命令行也行：`python -m ruyi_asr_shim --port 8791 --idle-unload-sec 60`。
另外三个子命令：`register` / `unregister` / `doctor`（`doctor` 打印环境自检，是**唯一**会主动 import torch 的入口）。

---

## 显卡加速

设备是**一档一档往下试**的，任何一档初始化失败都自动降到下一档，并在 stderr 说清原因。
**绝不会因为显卡后端坏了就整个服务起不来**——最差也是退到 CPU 继续干活。

顺序：`cuda/rocm` → `xpu` → `mps` → `directml` → `cpu`

### 英伟达卡（已在本机真机验证）

`scripts\install.ps1` 自动认卡并装 **cu128** 构建。RTX 50 系是 Blackwell（sm_120），
**必须 cu128 及以上**，老的 cu121／cu124 轮子在这块卡上起不来。

### AMD 卡 —— 已在 RX 7650 GRE 上真机验证（2026-09-24）

走 **AMD 官方的 ROCm on Windows** 轮子。好消息是代码不用分叉：在这条路上
`torch.cuda.is_available()` 就是 `True`、设备字符串还是 `"cuda"`，所以它落在**第一档**，
只是 `/health` 与日志里会如实显示成 `rocm` 而不是 `cuda`。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Gpu amd
```

前提（AMD 官方文档的原话，装不上时以官方页面为准）：

- **Python 3.12**——AMD 的 Windows 轮子只有 `cp312`，没有 3.11／3.13。（本 shim 缺省就是 3.12。）
- **显卡驱动 26.2.2 及以上。**
- 显卡在 AMD 的 Windows 支持列表里：RX 9070 / 9070 XT / 9060 XT、Radeon AI PRO R9700、
  RX 7900 XTX、RX 7700、PRO W7900，或 Ryzen AI Max+ 395 这类 gfx1150/1151 APU。
  **780M（gfx1103）不在 AMD 的正式支持列表里**（AMD 文档与 ROCm/TheRock 的清单口径冲突）。
  **实测：RX 7650 GRE（gfx1102）也不在列表里，但能跑**（见下面实测），同为 RDNA3 的
  7600／7600 XT 大概率一样，但没测过。
- dtype 缺省 **float16** 而不是 bfloat16：AMD 的 Windows 支持矩阵只承诺 FP16（RDNA4 另加 FP8），
  通篇没提 BF16，而且 gfx1100 上有过 bf16 相关的崩溃记录。想试：`RUYI_ASR_DTYPE=bfloat16`。
  实测 gfx1102 上 bf16 能跑、结果一样，但热态慢约 30%（0.80 s 对 0.61 s），所以缺省不变。

官方安装页：<https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installrad/windows/install-pytorch.html>

#### AMD 实测（2026-09-24，本机真跑）

机器：**AMD Radeon RX 7650 GRE 8 GB**（gfx1102，RDNA3）、Ryzen 5 5500、驱动 32.0.31041.1004、
Python 3.12.10、`torch 2.9.1+rocm7.2.1`（HIP 7.2.53211）、ROCm SDK 7.2.1、`transformers 5.17.0`。
按登记文件的 `run` 拉起（＝如意的做法），样例音频同上面英伟达那节。

| 量的是什么 | 实测 |
| --- | --- |
| 启动到 `/health` 就绪 | **0.57 s**；空转不占显存（0 MB） |
| 第一发（含加载 10.7 s） | **15.1 s** |
| 热态 · 中文 5.43 s | **0.61 – 0.72 s**，逐字正确 |
| 热态 · 英文 6.03 s | **0.80 – 0.87 s**（同样把 pull 听成 poll） |
| 热态 · 中文 + `language=zh` + 热词 | **0.50 – 0.51 s** |
| 长音频 45.8 s（切 2 段） | 这台机器上头一回 **25.7 s**，之后 **4.3 – 4.9 s**（重启进程也是；见下面第 2 条） |
| 卸载后重新加载的第一发 | **3.3 s**（加载 2.7 s） |
| 换 1.7B（请求带 `model`） | 第一发 **7.9 s**（加载 7.1 s），之后中文 0.63 – 0.67 s、英文 0.80 – 0.88 s |
| 进程显存 | 0.6B **≈ 2.1 GB**（长音频后 2.3 GB）；1.7B **≈ 4.8 GB**。8 GB 卡两份都装得下 |
| 模型占着显存时杀「父进程」 | **3.7 s** 后自退，退出码 0；整卡显存从 3971 MB 回到 1498 MB（＝本底），不留孤儿 |

**和英伟达不一样、要知道的几件事：**

1. **`/v1/unload` 之后进程还占约 1.1 – 1.5 GB 显存。** PyTorch 这一层是还干净了的
   （reserved 从 1644 MB 掉到 108 MB），剩下的是 Windows 上的 HIP 运行时自己留着不还给驱动。
   它不是泄漏：再加载会复用这块（换尺寸、重载都不往上涨）；**进程一退就全还**（看门狗那条实测回到本底）。
   对比：英伟达上卸载后只剩约 240 MB 的 CUDA 上下文。显存紧的话，只有让进程退出才能全拿回来（如意退出时会回收它）。
2. **一种新长度的音频，在一台机器上头一回会慢一截。** ROCm 第一次见到某种张量形状要现备内核：
   45.8 s 长音频头一回 25.7 s、之后 4.3 – 4.9 s。备好的内核会缓存在磁盘上，**重启进程后也不用再备**
   （第二轮实测新进程里同一条长音频直接 4.9 s，冷启第一发也从 15.1 s 降到 12.5 s）。
3. **stderr 里有两类噪声，不用管**：
   - `Claude: Unknown command line argument '…offload-arch.exe'` —— 安装路径里有空格（如 `Claude Code`）时，
     ROCm SDK 探测显卡型号用的 `os.execv` 在 Windows 上不给带空格的路径加引号。探测失败后它退回装好的那套库，
     不影响推理；装在不带空格的路径下就没这行。
   - `Flash/Mem Efficient attention on Current AMD GPU is still experimental` —— PyTorch 自动退回普通注意力实现，结果不受影响。

**没验证的**：本次没重跑 `install.ps1 -Gpu amd`（实测用的环境与脚本钉的版本逐项一致：ROCm SDK 7.2.1 四个包＋
`torch 2.9.1+rocm7.2.1`）；AMD 的**离线打包**（`tools/package-bundle.ps1` 带 ROCm 依赖）没在 AMD 目标机上装过；
支持列表里的其它卡、RDNA4、APU 都没测。卡不在支持列表、又跑不起来，就用 CPU（见下），别在 AMD GPU 上死磕。

### 为什么不推荐 torch-directml

它把 torch 钉死在 `torch==2.4.1`，而 transformers 5.x 要 `torch>=2.5` —— **pip 直接解不开**；
微软自己在 DirectML 的 README 顶上写着 "in maintenance mode"；而且明确不支持 bfloat16。
代码里那一档还留着（万一你自己装了个能用的环境），但**安装脚本不装它**。
详见 [`docs/backend-notes.md` §8](docs/backend-notes.md)。

### 没有可用显卡：CPU

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Gpu cpu
```

能跑，但慢很多（本机没有量 CPU 的具体数字，只在启动日志里会明确告诉你「现在是 CPU 推理，会很慢」）。

---

## 已知限制

1. **超过 30 秒的音频会被切段。** Qwen3-ASR 的音频编码器窗口是 30 秒，本 shim 自动按 28 秒
   切窗口顺序转、再把文本接起来。**切口不看句读，可能吞字** —— 实测 39 秒那条把
   「另外提醒一下」转成了「另外提。请一下」。如意麦克风那一路是按停顿切段的（2–30 秒），
   走不到这条分支；长音频附件会。
2. **m4a / mp4 / webm 解不了，回 415。** 这台机器没有 ffmpeg，本 shim 也不装系统级软件。
   能解的是 **wav / flac / ogg**，以及多数情况下的 **mp3**（靠 soundfile 自带的 libsndfile，
   本机是 1.2.2，带 MPEG 解码）。解不了的时候回一句人话让你转成 wav。
   **麦克风那一路（16 kHz 单声道 WAV）是必保的主路径**，它连 soundfile 都不需要，标准库就能解。
3. **回体里没有 `usage`。** 本地推理没有可信的 token 账，编一个不如不报。
   如意那边会自己按字节数估算，并在账本里标 `estimated: true`。
4. **强制语言时回体里没有 `language` 字段。** 这是模型本身的行为（你指定了它就不再自己报），
   如意对缺 `language` 是容忍的。
5. **不做流式**（没有 WebSocket 边录边出字）。如意已经用「按停顿切段」达到了边说边出字的效果。
6. **不做 `/v1/chat/completions` 的 `input_audio` 形**（如意的「对话型」协议）。打过来会回 404。
7. **一次只转一段**（一把锁串行）。并发打进来会排队，不会并行 —— 0.6B 在这块卡上远快于实时，
   并发没有收益只有显存风险。

---

## 排障

| 症状 | 多半是 |
| --- | --- |
| 如意没发现它 | 登记文件在不在？`type %USERPROFILE%\.ruyi-toolbox\components\asr-shim.json`。不在就跑 `download-model.ps1`，或者手动 `python -m ruyi_asr_shim register --model-dir <模型目录>` |
| `register` 说自检没过 | 它会逐条告诉你哪一条不过（模型目录不在／没权重／工作目录不对）。**自检不过就绝不写登记文件** —— 文件存在就等于让如意去执行它 |
| 起不来，说端口用不了 | 端口被占。`RUYI_ASR_PORT=8791` 换一个。如意自己会挑空闲端口，这个问题只在手动起的时候有 |
| 转写很慢（几十秒） | 在用 CPU。`python -m ruyi_asr_shim doctor` 看「会用的设备」是什么，以及为什么没用上显卡 |
| 回 415 | 音频格式解不了（m4a/webm）。转成 wav |
| 回 403 | `Host` 头不是 `127.0.0.1:<端口>`／`localhost:<端口>`，或者请求带了 `Origin` 头。这是防浏览器跨站打本机端口的闸，不会放宽 |
| 第一次转写等了十几秒 | 正常，那是在加载模型（冷文件缓存下实测 14.5 s）。之后就是半秒级。不想等可以 `RUYI_ASR_IDLE_UNLOAD_SEC=0` 让它常驻 |
| 如意退了但显存还被占着 | 不该发生 —— 有父进程看门狗。真遇到了请把 stderr 日志留下来 |

## 停用与卸载

- **临时停用**：在如意的设置页里把这个组件关掉（如意侧的开关，不删登记文件）。
- **不让如意再管它**：`.venv\Scripts\python.exe -m ruyi_asr_shim unregister`（删登记文件）。
- **彻底卸载**：先 `unregister`，再删掉 `asr-shim\.venv` 与 `asr-shim\models`。
  本组件**不写注册表、不设开机自启**，除了那一份登记文件之外不在安装目录外留任何东西。

## 自己跑测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

**不加载真模型、不需要 GPU、不联网**（引擎是可注入的，测试用假后端）。

## 更多

- [`docs/backend-notes.md`](docs/backend-notes.md) —— Qwen3-ASR 真实 API、依赖版本、AMD 加速的查证笔记（含来源与日期）
- [`../docs/00-component-registry.md`](../docs/00-component-registry.md) —— 组件怎么向如意登记自己
- [`../docs/01-asr-shim-plan.md`](../docs/01-asr-shim-plan.md) —— 本组件的方案与交办单
