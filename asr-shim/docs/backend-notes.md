# 后端查证笔记（Qwen3-ASR ＋ 设备加速）

> 查证日期：**2026-09-21**。方法：读官方 GitHub README、HuggingFace／ModelScope 模型卡、
> PyPI 的 JSON 元数据、transformers 源码。凡是**没查到来源的一律标「查不到」**，不凭印象写。
> 与 `docs/01-asr-shim-plan.md` §2.5 里「出方案的人未核实的印象」不符之处，本文列在 §6。

---

## 1. 模型仓库：要用带 `-hf` 后缀的那一份

| 仓库 | 给谁用 | 权重大小 |
| --- | --- | --- |
| `Qwen/Qwen3-ASR-0.6B-hf` | **原生 transformers（本 shim 走这条）** | `model.safetensors` 1,564,928,088 B ≈ 1.46 GiB |
| `Qwen/Qwen3-ASR-0.6B` | 官方 `qwen-asr` 包 | 1,876,091,704 B ≈ 1.75 GiB |
| `Qwen/Qwen3-ASR-1.7B-hf` / `Qwen/Qwen3-ASR-1.7B` | 同上，更大的那个尺寸 | — |
| `Qwen/Qwen3-ForcedAligner-0.6B(-hf)` | 只做时间戳对齐，不是 ASR | — |

- **真实存在的 ASR 尺寸只有 0.6B 与 1.7B**（外加一个对齐模型）。HF 与 ModelScope 上
  这六个仓库的 API 探测都返回 200。
- 两份仓库的 `config.json` 都写 `architectures: ["Qwen3ASRForConditionalGeneration"]`，
  但**结构不同**：`-hf` 那份是扁平的 `audio_config` + `text_config`（`model_type: qwen3_asr`，
  `transformers_version: 5.13.0.dev0`）；不带 `-hf` 的那份是 `thinker_config` 嵌套结构，
  而 transformers 的 `configuration_qwen3_asr.py` 里没有任何 `thinker` 处理逻辑。
  **结论：不带 `-hf` 的仓库不要喂给 AutoModel。**（此条是从 config 结构与源码推断，官方没有明文，
  但 News 里把 `-hf` 三个仓库单列为 "Native Transformers support" 的模型卡，佐证一致。）
- 权重大小的差值 311 MB 正好是 `151936 × 1024 × 2` 字节 —— `-hf` 那份 `tie_word_embeddings: true`
  省掉了 `lm_head`。

来源：
- https://github.com/QwenLM/Qwen3-ASR （README News：「2026.6.26 Native Transformers support is now available!」列出三个 `-hf` 模型卡）
- https://huggingface.co/Qwen/Qwen3-ASR-0.6B-hf
- https://modelscope.cn/models/Qwen/Qwen3-ASR-0.6B-hf
- HF API `https://huggingface.co/api/models/<id>?blobs=true`（权重字节数）

## 2. 调用形状（本 shim 逐行照此实现）

模型卡原文的最小可运行示例：

```python
from transformers import AutoProcessor, AutoModelForMultimodalLM

model_id = "Qwen/Qwen3-ASR-0.6B-hf"
processor = AutoProcessor.from_pretrained(model_id)
model = AutoModelForMultimodalLM.from_pretrained(model_id, device_map="auto")

inputs = processor.apply_transcription_request(audio=...).to(model.device, model.dtype)
output_ids = model.generate(**inputs, max_new_tokens=256)
generated_ids = output_ids[:, inputs["input_ids"].shape[1]:]

parsed = processor.decode(generated_ids, return_format="parsed")[0]
# {'language': 'English', 'transcription': 'Mr. Quilter is ...'}
```

- **transformers 最低版本 5.13.0**（模型卡原文：「Qwen3-ASR is supported natively in 🤗 Transformers,
  starting from v5.13.0」）。
- **不需要 `trust_remote_code`**：两个仓库的文件列表里没有任何 `.py`，类已经内置在 transformers 里，
  auto 映射 `("qwen3_asr", "Qwen3ASRForConditionalGeneration")` 已注册。
- `processor.decode(..., return_format=...)` 有三种：`"raw"`（`language English<asr_text>…`）、
  `"parsed"`（dict）、`"transcription_only"`（纯文本）。本 shim 用 `parsed`，并对老版本回退到解 raw。
- 也有 `pipeline` 写法，但 task 是 `"any-to-any"` 且还要手动 `extract_transcription`，对做
  OpenAI 兼容服务不划算 —— 不用。

## 3. 音频输入：**必须我们自己保证 16 kHz 单声道**

`transformers/audio_utils.py` 的 `load_audio()` 第一行是：

```python
if isinstance(audio, np.ndarray):
    return audio
```

即**传 ndarray 时它不做任何重采样、不转单声道、不检查采样率**。传 path／URL 才会按 16000 解码。

`processor_config.json` 的 feature extractor：`sampling_rate 16000`、`feature_size 128`、
`hop_length 160`、`n_fft 400`、`chunk_length 30`、`n_samples 480000`。

对本 shim 的三个直接后果：

1. `audio.py` 负责把任意输入解成 **16 kHz 单声道 float32**，再交给模型。
2. **完全不需要 ffmpeg／torchcodec** —— transformers 的音频读取分支（torchcodec ／ librosa）我们
   根本走不到。这正合这台没有 ffmpeg 的机器。
3. 音频编码器窗口是 30 秒。超过 30 秒的音频本 shim 自己切窗口顺序转、把文本接起来
   （`qwen_backend._split` / `_join`）。**切在哪里由时长决定，不看句读，所以长音频的切口处可能断词**
   —— 这是已知限制，写在 README。

## 4. 语言、热词

- `language=None`（默认）＝自动识别，模型自己在输出里带 `language <NAME>`。
- 强制语言：`language="Chinese"` 或 ISO 码 `"zh"`（docstring 原文：「Accepts full names (e.g. "English",
  "Chinese") or ISO codes (e.g. "en", "zh")」）。如意传过来的是 OpenAI 形的 ISO 码，直接透传。
- **强制语言时 `parsed["language"]` 可能是 None**（模型卡示例里自己写了 `parsed["language"] or "English"`）。
- 热词／领域词：transformers 路径的参数叫 **`prompt`**（"Context/hotwords to include as the system prompt"）；
  官方 `qwen-asr` 包里叫 `context`。如意的 OpenAI 形 `prompt` 字段正好对上，直接透传。
- **ITN（逆文本正则）／标点开关：查不到。** README 与两份模型卡里 grep `ITN` / `inverse text` /
  `normaliz` / `punctuat` 全部零命中。没有这类参数。（没读 arXiv:2601.21337 正文，不排除论文里有。）
- 时间戳要另外加载 `Qwen3-ForcedAligner-0.6B-hf`，不在本 shim 范围。

## 5. 依赖与版本

| 东西 | 事实 | 来源 |
| --- | --- | --- |
| `transformers` | 原生支持 ≥ 5.13.0；最新 5.17.0；`requires_python >=3.10.0`，classifiers 含 3.13/3.14 | 模型卡、PyPI JSON |
| torch cu128 | Windows 有 cp313 轮子：2.7.0 / 2.7.1 / 2.8.0 / 2.9.0 / 2.9.1 / 2.10.0 / 2.11.0（还有 cp313t、cp314） | https://download.pytorch.org/whl/cu128/torch/ |
| `modelscope` | 最新 1.40.1，`requires_python >=3.10`，纯 Python `py3-none-any` 轮子 → 3.13 能装（classifiers 只列到 3.12，但那不影响安装） | https://pypi.org/pypi/modelscope/json |
| 官方 `qwen-asr` 包 | 存在，最新 0.0.6（2026-01-30）。`requires-python >=3.9`，**但把 `transformers` 钉死在 `==4.57.6`** | https://pypi.org/pypi/qwen-asr/json |

> **关键坑**：`qwen-asr` 的 `transformers==4.57.6` 与原生路径要的 `transformers>=5.13.0` **互斥**，
> 两条路线不能装在同一个环境里。本 shim 走原生 transformers，**不装 `qwen-asr`**。

本 shim 选 **Python 3.12**：官方 README 自己推荐的就是 3.12（`conda create -n qwen3-asr python=3.12`），
而 3.13 上 `soundfile`／`soxr` 这类带扩展的轮子可用性我没有逐个查证过。3.13 没有已知硬阻塞，
只是没有必要冒这个险 —— `uv venv --python 3.12` 会自己把解释器下下来，不动系统 Python。

## 6. 与方案 §2.5 那份「未核实的印象」的出入

| 方案 §2.5 的印象 | 查证结果 |
| --- | --- |
| 有 `qwen-asr` 这个 PyPI 包，仓库 `QwenLM/Qwen3-ASR` | **对**。包名 `qwen-asr` 0.0.6，仓库确实存在。 |
| 用法是 `Qwen3ASRModel.from_pretrained(path, dtype=…, device_map=…)` 然后 `.transcribe(audio=…, language=None)` 取 `results[0].text` / `.language` | **对**，但那是 `qwen-asr` 包的 API。**方案说「只用 transformers 后端」，而原生 transformers 路径的 API 完全不同**（`AutoProcessor.apply_transcription_request` + `model.generate` + `processor.decode`）。本 shim 走后者。 |
| 「另有 vLLM 后端」 | **对**（`qwen-asr[vllm]`，钉 `vllm==0.14.0`）。不用。 |
| 模型是 `Qwen/Qwen3-ASR-0.6B` | **半对**。这个仓库存在，但**给裸 transformers 用要 `-hf` 后缀的那份**。这是本次查证最重要的一条更正。 |
| 「先看官方包自己怎么读音频（多半是 librosa／soundfile）」 | `qwen-asr` 包确实依赖 librosa + soundfile + sox；但原生 transformers 路径下，**我们传 ndarray 就绕开了它全部的音频读取逻辑**，所以本 shim 只需要 soundfile（还只在非 WAV 时才用到）。 |

## 7. 官方没给的数字

- **0.6B 的显存占用：官方查不到。** README 与两份模型卡里没有任何 VRAM 数字，只有
  「we recommend using FlashAttention 2 to reduce GPU memory usage」这种定性说法。
  第三方估算（Spheron，非官方）给 ~2 GB FP16。本 shim README 里写的是**本机实测值**。
- **RTX 5080 / Blackwell sm_120 上跑 Qwen3-ASR 的第三方实测报告：查不到。**
- **FlashAttention 2 在 Windows + cu128 上的可用性：未查证**（通常要自行编译）。本 shim 用默认 attention，
  不去碰它。

---

## 8. AMD／非英伟达加速（2026-09-21 查证）

一句话：**AMD 走「ROCm on Windows 的官方 PyTorch 轮子」，代码不用为它分叉；
`torch-directml` 是死路，别试。**

### 8.1 AMD 官方 Windows 版 ROCm PyTorch —— 唯一活路

来源：<https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installrad/windows/install-pytorch.html>
（文档自述覆盖到 ROCm 7.2.1）

- 当前组合：**ROCm 7.2.1 + torch 2.9.1+rocm7.2.1**，轮子从 `https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/` 直接 pip 装
  （先四个 `rocm_sdk_*` 包，再 `torch-2.9.1+rocm7.2.1-cp312-cp312-win_amd64.whl`）。
- **硬性前提（官方原文）**：「Python 3.12 must be installed」、「the 26.2.2 graphics driver must be installed」。
  轮子文件名是 `cp312-cp312` —— **只有 Python 3.12 这一个版本，没有 3.11/3.13**。
  （这正好和本 shim 选 3.12 对上，见 §5。）
- **支持的显卡（Windows 11，ROCm 7.2.1）**：
  - 独显／工作站：RX 9070、RX 9070 XT、RX 9060 XT、Radeon AI PRO R9700（RDNA4，gfx1200/1201）；
    RX 7900 XTX、RX 7700、Radeon PRO W7900 / W7900 Dual Slot（RDNA3，gfx1100/1101）。
  - Ryzen APU：Ryzen AI Max+ 395、Max 390/385、AI 9 HX 375/370/475/470、AI 9 365/465（gfx1150/1151）。
  - **780M（gfx1103）不在 AMD 正式支持矩阵里**（TheRock 的 `SUPPORTED_GPUS.md` 标它 Release Ready，
    两处口径冲突，查不到哪个是当前真相）。
  - 全站通用警告（官方原文）：「PyTorch on Windows includes ROCm 7.2 components; however,
    the entire ROCm stack is not yet fully supported on Windows.」
- **对本 shim 的代码意味着什么：不用分叉。** AMD 官方安装页的验证章节明确
  `torch.cuda.is_available()` 返回 **True**，GPU 名从 `torch.cuda.get_device_name(0)` 取。
  在这条路上 `torch.version.hip` 非 None、`torch.version.cuda` 为 None、设备字符串**仍然是 `"cuda"`**。
  所以 `devices.probe_cuda` 一档同时覆盖英伟达与 AMD，只在 label／`/health` 里区分 `cuda` 与 `rocm`。
- **dtype：缺省 float16，不用 bfloat16。** AMD 那份 Windows 支持矩阵的「AI Data Types」只列了
  `FP16` 和 `FP8 (Supported only on RDNA4 GPUs)`，**整页没有出现 BF16**；另有 PyTorch issue #165141
  报告 gfx1100 上 MIOpen + bfloat16 的严重故障。ROCm 通用精度文档确实把 bf16 列为库级支持，
  但那不构成对这条 Windows 路线的背书。要试可以 `RUYI_ASR_DTYPE=bfloat16`。
- **稳定性风险**：TheRock issue #5543（2026-05-31）报告 Windows + gfx1100 + nightly 轮子下
  所有 GPU 算子报 `HIP error: device kernel image is invalid`（已标 fix submitted）。
  → **用 repo.radeon.com 的正式发布轮子，别用 nightly。** `probe_cuda` 里那个「真的分配一小块显存」
  的动作就是为这类故障准备的：早炸早降级，不要加载到一半才崩。

### 8.2 torch-directml —— 死路，本项目不装它

| 证据 | 内容 |
| --- | --- |
| 版本钉死 | PyPI `torch-directml` 最新 **0.2.5.dev240914（2024-09-15）**，`requires_dist` 写 **`torch==2.4.1`** |
| 与 transformers 冲突 | transformers 5.x 的 `setup.py` 原文是 `"torch>=2.5"` → 与 `torch==2.4.1` **直接矛盾，pip 解不开** |
| 维护状态 | `microsoft/DirectML` README 顶部：「⚠️ **DirectML is in maintenance mode** ⚠️」，只收安全与合规修复，不加新功能 |
| bfloat16 | DirectML issue #688：「Invalid or unsupported data type BFloat16」，**closed as not planned** |

结论：三重死。`devices.probe_directml` 仍然留着（万一有人自己装了一个能用的环境），
但**安装脚本不装它，README 也不推荐它**，AMD 用户请走 §8.1。

### 8.3 其它路（都不适合「transformers 模型直接 generate」）

- **ONNX Runtime DirectML EP / Windows ML**：要先把模型导成 ONNX，`generate()` 不能直接跑。
  Windows ML 是微软给 DirectML 的继任者，方向对，但要换整条推理栈。
- **OpenVINO / optimum-intel**：Intel 自家硬件，对 AMD 独显无意义。
- **Vulkan**：PyTorch 的 Vulkan 后端早已不是推理可行路径，transformers 也不认。

### 8.4 transformers 自己的设备抽象

- `device_map="auto"` 的实现在 accelerate 里，走的是**已注册的 PyTorch 后端**（CUDA/XPU/MPS/NPU…），
  **不认识 DirectML**（那是 `privateuseone` 类外部后端）。
- transformers v5 quicktour 官方推荐的通用写法是 `Accelerator().device`，不是 `torch.accelerator`。
- `torch.accelerator.current_accelerator()` 在 AMD ROCm 上返回 `device(type='cuda')` —— 和
  `torch.cuda.is_available()` 是同一件事，不是额外一条路。
- 本 shim 不用 `device_map="auto"`：我们自己挑设备、自己 `.to()`，这样降级逻辑才在我们手里。

### 8.5 本项目对 AMD 的验证状态 —— **已在一块 AMD 卡上真机验证（2026-09-24）**

最初的开发机是 RTX 5080 Laptop，当时 AMD 路径只有假探针单测（`tests/test_devices.py`）和逐条对照官方文档的安装命令。
2026-09-24 在 **RX 7650 GRE 8 GB（gfx1102，RDNA3）**、驱动 32.0.31041.1004、`torch 2.9.1+rocm7.2.1`、ROCm SDK 7.2.1
（与 `install.ps1 -Gpu amd` 钉的版本逐项一致）上按登记文件真跑，数字见 README「AMD 实测」。结论：

1. **单测里的判断都成立**：`torch.cuda.is_available()` 为真、设备字符串是 `"cuda"`、`torch.version.hip` 有值
   → 落在第一档并显示成 `rocm`；dtype 缺省 float16。`get_device_capability()` 在 gfx1102 上回 `(11, 0)`，
   所以 `doctor` 在 ROCm 上改报 `gcnArchName`（`gfx1102`），不再印出 `sm_110` 这种英伟达写法。
2. Qwen3-ASR 0.6B／1.7B 在 ROCm 上用 `AutoModelForMultimodalLM` + `generate()` **原样可跑**，中文逐字正确，不需要改代码。
3. **卸载后的残留显存与英伟达不同**：`empty_cache()` 之后 PyTorch reserved 掉到约 108 MB，但 Windows 上的
   HIP 运行时仍然给进程留着约 1.1 – 1.5 GB，不还给驱动；再加载会复用，进程退出全部归还。
   单独的最小实验（只建上下文、跑一次 fp16 matmul／SDPA／conv1d、再分配又释放 1 GB）残留只有约 350 MB，
   说明多出来的部分跟真实模型的分配模式有关，不是 shim 持有引用（`gc` 里找不到任何存活的 CUDA 张量）。
4. 新形状首次执行要现备内核（45.8 s 长音频头一回 25.7 s、之后 4.3 – 4.9 s）；备好的内核跨进程有磁盘缓存，第二轮在新进程里同一条长音频直接 4.9 s、冷启第一发 15.1 s → 12.5 s。
5. ROCm SDK 的 `rocm_sdk._dist_info.discover_current_target_family()` 经 `Scripts\offload-arch.exe`
   → `rocm_sdk_core._cli._exec()` → `os.execv()` 调 LLVM 的 `offload-arch`；Windows 上 `os.execv` 不给带空格的
   argv 加引号，路径含空格时 stderr 会冒一行 `Unknown command line argument`，随后退回已装的库，不影响推理。
6. **真机才测出来的 bug（已修）**：Windows ROCm 上，进程发了 GPU 内核、没 `synchronize` 就退出，会**卡在退出阶段不走**
   （最小复现：`torch.zeros(8, dtype=torch.float16, device="cuda"); del p` 后退出——挂住；中间加一句
   `torch.cuda.synchronize()`——3 s 正常退出）。`devices.probe_cuda` 的显存探针正好是这个形状，于是 `doctor` 打印完就挂住，
   `install.ps1 -Gpu amd` 最后一步「自检」会跟着挂。修法是探针里分配完立刻 `synchronize()`——顺带让异步报出的内核错误
   也落进探针的 `try`，按原意降级。服务进程本身不受影响（它靠看门狗／被杀退出，实测 3.7 s 自退）。

**仍未验证**：`install.ps1 -Gpu amd` 这次没有从零重跑；AMD 离线打包没在 AMD 目标机上装过；其它 AMD 卡、RDNA4、APU 没测。
网上仍然找不到第三方的 Qwen3-ASR on ROCm 实测报告。

### 8.6 §8 的不确定项

1. ~~bfloat16 在 Windows ROCm PyTorch 上到底能不能用~~ —— gfx1102 实测能用、结果与 fp16 一致，但热态慢约 30%
   （0.80 s 对 0.61 s）；缺省保持 float16。其它架构（尤其 gfx1100 的崩溃记录）仍未知。
2. 780M（gfx1103）的真实状态 —— AMD 正式文档与 TheRock 的清单冲突。gfx1102 同样不在正式列表里，但实测能跑。
3. `torch.version.hip` 在 Windows 轮子上的确切返回格式 —— 安装页显示「HIP runtime version 7.2.53211」，
   但没找到逐字写出 `torch.version.hip` 返回值的官方片段。本 shim 的代码只判它是不是 None，
   不解析格式，所以这条不影响正确性。
4. ROCm 10.0.0 是否已带来更好的 Windows 支持 —— radeon-ryzen 子项目文档仍停在 7.2.1，关系没查清。

### 8.7 §8 的来源

- <https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installrad/windows/install-pytorch.html>
- <https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityrad/windows/windows_compatibility.html>
- <https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/compatibility/compatibilityryz/windows/windows_compatibility.html>
- <https://github.com/ROCm/TheRock/blob/main/RELEASES.md> ／ <https://github.com/ROCm/TheRock/blob/main/SUPPORTED_GPUS.md> ／ issue #5543
- <https://github.com/pytorch/pytorch/issues/165141>（gfx1100 bf16 故障）
- <https://rocm.docs.amd.com/en/latest/reference/precision-support.html>
- <https://pypi.org/pypi/torch-directml/json> ／ <https://github.com/microsoft/DirectML> ／ DirectML issue #688
- <https://raw.githubusercontent.com/huggingface/transformers/main/setup.py>（`"torch>=2.5"`）
- <https://huggingface.co/docs/transformers/main/en/quicktour>
- <https://docs.pytorch.org/docs/2.14/accelerator.html>
