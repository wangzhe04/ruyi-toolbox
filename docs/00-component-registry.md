# 00 · 组件登记约定（ruyi-toolbox ⇄ 如意工作台）

> 2026-09-21，用户拍板：「所有 ruyi-toolbox 下的都是这样 —— 是 MCP 就自动配上，其它工具也自动识别接入」，目标是开箱即用。
> 本文是两个仓库之间**唯一**的对接面：toolbox 组件只管按本文登记自己；如意只管读登记、不读任何组件的代码目录。

## 1. 一句话

组件装好之后在 `~/.ruyi-toolbox/components/` 下放一份登记文件；如意启动时扫这个目录，
**是服务就拉起并接成对应能力的端点，是 MCP 就加进 MCP 服务器清单**。卸载＝删掉那份登记文件。

## 2. 登记文件

- 位置：`~/.ruyi-toolbox/components/<id>.json`（Windows：`%USERPROFILE%\.ruyi-toolbox\components\<id>.json`）。
- 编码：UTF-8 **无 BOM**、LF。写法：先写同目录临时文件再 rename（原子）。这是组件在自己安装目录之外**唯一**允许写的地方。
- `<id>`：`[a-z0-9][a-z0-9-]{0,39}`，与文件名一致，全 toolbox 唯一。
- **登记文件存在＝如意会去执行它**。所以登记之前组件必须自检（可执行文件在、依赖装好、必要的模型／数据在），
  自检不过就不登记并说清原因。不许登记一个起不来的东西。
- 如意对看不懂的 `schema`、看不懂的 `kind`、校验不过的文件一律当作「没装」并记一条日志，不报错打断启动。

### 2.1 公共字段

```json
{
  "schema": 1,
  "id": "asr-shim",
  "kind": "service",
  "name": "本地语音识别（Qwen3-ASR）",
  "version": "0.1.0",
  "run": {
    "command": "E:\\Claude\\ruyi-toolbox\\asr-shim\\.venv\\Scripts\\python.exe",
    "args": ["-m", "ruyi_asr_shim"],
    "cwd": "E:\\Claude\\ruyi-toolbox\\asr-shim",
    "env": { "RUYI_ASR_MODEL_DIR": "E:\\Claude\\ruyi-toolbox\\asr-shim\\models\\Qwen3-ASR-0.6B" }
  },
  "registeredAt": "2026-09-21T00:00:00.000Z"
}
```

- `run.command`／`run.cwd`：**绝对路径**，且必须存在。如意不经 shell 执行（`spawn(command, args, { shell: false })`），
  不做任何字符串拼接，不展开环境变量。`args` 是字符串数组（≤ 32 项、每项 ≤ 1000 字符），`env` 是字符串到字符串的表（≤ 32 项）。
- `name`：给人看的名字（≤ 80 字符），出现在如意的设置页与「发现了新组件」的提示里。
- 所有可配项必须能从 `args`／`env` 给到 —— 如意不会往命令行后面追加参数。

### 2.2 `kind: "service"` —— 常驻本机的小 HTTP 服务

```json
{
  "kind": "service",
  "service": {
    "port": 8790,
    "portEnv": "RUYI_ASR_PORT",
    "health": "/health",
    "component": "ruyi-asr-shim"
  },
  "provides": [
    { "type": "asr", "basePath": "/v1", "model": "qwen3-asr-0.6b", "protocol": "transcriptions" }
  ]
}
```

两个可选字段（2026-09-21，第 133 波；老版本如意不认就忽略）：

- `service.unload`：一条 POST 路径（如 `/v1/unload`）。用户在如意里把语音识别**切走**（换模型、换服务商、关掉）时，如意立刻打它，
  组件应当就地卸载已加载的模型、释放显存，回 `{"ok": true, "unloaded": <bool>}`。没这个字段就只靠组件自己的空闲卸载。
- `provides[].models`：`[{ "id", "label" }, …]`，同一个端点上可选的多份模型（如 asr-shim 的 `qwen3-asr-auto` / `qwen3-asr-0.6b` /
  `qwen3-asr-1.7b`）。给了就按它画清单（`label` 是设置页那一格显示的话，例如「1.7B（更准，约 5 GB 显存）」）；`model` 仍是缺省那一份，
  且必须在清单里。转写请求带哪个 `model`，组件就用哪份（换了就换加载）。

如意怎么对待它：

1. 先探 `http://127.0.0.1:<port><health>`：已经活着、且回体 JSON 里 `component` 等于登记的 `service.component`
   → 直接用（用户手动起的），**不再拉第二个**。
2. 没活着 → 拉起。端口被占（探到的不是本组件）→ 如意另挑一个空闲端口，经 `portEnv` 指的那个环境变量告诉组件。
   **组件必须以环境变量里的端口为准**，登记的 `port` 只是首选。
3. 同时传 `RUYI_TOOLBOX_PARENT_PID=<如意的 pid>`。
4. 健康轮询 ≤ 20 秒；起不来就放弃、记日志、在设置页如实显示「没起来」和 stderr 的最后几行，不重试到天荒地老。
5. 如意退出时杀掉它拉起的子进程（整棵进程树）。不是它拉起的（第 1 条那种）不杀。
6. 按 `provides` 接能力。现在认识的 `type`：
   - `asr`：语音识别端点。如意生成一个服务商条目（地址 `http://127.0.0.1:<实际端口><basePath>`、无密钥、
     模型 `model` 带语音识别标记、`protocol` 取 `transcriptions` 或 `chat-audio`）。**用户还没配过语音识别时自动选中它；
     已经配了别的就只把它列为候选，绝不改用户的选择**；用户后来手动关掉或换走，如意也不会再自动选回来。
   - `asr-stream`（2026-09-21，第 130 波）：**流式**语音识别端点，给输入框麦克风「边说边出字」用。如意生成一个服务商条目
     （地址 `http://127.0.0.1:<实际端口><basePath>`、无密钥、模型带 `asr-stream` 标记），自动选中规则与 `asr` 同一套
     （`asrStreamProviderId`／`asrStreamModel`）。接口是「有会话的 HTTP」而不是 WebSocket，见 `docs/02-asr-stream-plan.md` §2：
     `POST {basePath}/stream/sessions` → `{id}`；`POST …/sessions/{id}/audio`（16 kHz PCM16LE，单块 ≤ 1 MB）→
     `{partial, finals:[{text,startMs,endMs}]}`；`POST …/finish`；`DELETE …/{id}`。`protocol` 字段不用。
   - **一个组件可以同时提供多种**（2026-09-21，131c）：`asr-stream` 组件配了 SenseVoice 后 `provides` 里同时有 `asr-stream` 与 `asr`，
     如意生成**一个**服务商条目、两个带不同标记的模型；两对配置键各自按上面的规则自动选中。
   - 以后会加 `tts`、`embedding`、`ocr` 等；不认识的 `type` 如意直接跳过（向前兼容），所以组件可以先登记、如意后支持。

服务类组件必须守的行为：

- **只绑 `127.0.0.1`**；无鉴权的前提是仅本机可达，所以要拒掉 `Host` 不是 `127.0.0.1:<port>`／`localhost:<port>` 的请求
  （防 DNS rebinding）和带 `Origin` 头的请求（防网页跨站打本机端口）。
- `health` 回 200 的 JSON，至少含 `{"ok": true, "component": "<同登记>", "version": "…"}`；探活不许触发重活（加载模型之类）。
- **父进程看门狗**：给了 `RUYI_TOOLBOX_PARENT_PID` 就每 ≤ 5 秒看一眼那个 pid 还在不在，不在了自己干净退出。
  如意被强杀／崩溃时第 5 条来不及执行，靠这条不留孤儿（占着显存的孤儿尤其不行）。Windows 上判 pid 存活要用可靠办法
  （`OpenProcess`＋`WaitForSingleObject`／`GetExitCodeProcess`，或 psutil），不要解析 `tasklist` 输出。
- **空转要轻**：如意一启动就会把它拉起来，绝大多数时间它在空转。重依赖（torch 之类）必须等第一发真请求再 import／加载。
- 端口被占等启动失败要**立刻非零退出**并在 stderr 留一句人话，不要挂着重试。
- 不弹窗口、不读 stdin；日志走 stderr，只记元数据（如意会把它接进自己的日志，所以不得含用户内容）。

### 2.3 `kind: "mcp"` —— 标准 stdio MCP server

```json
{
  "kind": "mcp",
  "mcp": { "transport": "stdio" }
}
```

`run` 就是 MCP server 的启动命令。如意把它作为一个外部 MCP 服务器接入（内部 id `toolbox-<id>`，显示名用 `name`），
生命周期、工具清单、权限分级全部走如意现有的 MCP 机制 —— 组件不需要、也不应该知道如意内部怎么管 MCP。
只支持 `stdio`。工具要给模型用的东西，一律做成 MCP；不要发明第三种接法。

### 2.4 「其它工具」怎么算

如意里只有两种接入面：**给模型用的工具＝MCP**；**给如意自己用的能力端点＝service＋provides**。
一个组件想同时提供两样，就登记两份文件（两个 id）。脚本、一次性命令行小工具不属于「接入」，不用登记。

## 3. 安全边界（两边都要守）

- 登记目录在用户主目录下，与如意自己的 `config.json` 同一个信任域：能写这里的人本来就能改如意的配置。
  如意不提供任何经 HTTP API 写登记文件或改其中命令的途径 —— **命令只来自磁盘上的文件**（主仓 26 号文 §4 的原话）。
- 如意每次拉起／接入都记审计日志（组件 id、命令、pid、结果）；第一次见到某个组件会给用户一条看得见的提示
  「发现并接入了 <name>」，设置页能逐个停用，也能整体关掉自动发现。**自动，但不是悄悄。**
- 停用是如意一侧的开关，不删登记文件；卸载才删。

## 4. 给组件作者的清单

1. 组件自带 `register`／`unregister` 两个动作（子命令或脚本），安装脚本最后调 `register`。
2. `register` 先自检、再原子写；`unregister` 删文件（文件不在也算成功）。
3. 按 §2.2／§2.3 守行为。4. README 写清：装完会登记、如意会自动接入、怎么停用、怎么卸载。
5. 登记文件里不放任何密钥。
6. 想让组件能被 `tools/package-bundle.ps1` 自动发现、正确打包（换机器不用手敲安装步骤），照 §6 的清单来。

## 5. 边界情况怎么算（2026-09-21 联调后补）

- **文件名与 `id` 不一致**：如意当作没装（不猜哪个对）。组件的 `register` 必须保证两者一致。
- **登记还在、东西没了**（整个目录被挪走／改名、虚拟环境被删）：`run.command`／`run.cwd` 不存在 → 如意当作没装并记一条日志，
  不会崩、也不会去执行别的东西。修法是在新位置重跑组件的 `register`。
- **自动生成的服务商会出现在如意的服务商列表里**：只提供 `asr` 的组件没有对话接口，被选去对话会得到一个 404。
  这是如意一侧待办（让只做语音的条目不进对话模型候选），组件不用为此做任何事。
- **Windows 上 venv 的 `python.exe` 是转发壳**：进程表里会有两个 python 进程。如意回收时杀整棵树；组件自己的父进程看门狗是第二道保险。

## 6. 打包与迁移（`tools/package-bundle.ps1`，2026-09-21 起）

装好的组件要挪去另一台机器，不用逐个手敲 `install.ps1`／`download-model.ps1`：根目录的
[`tools/package-bundle.ps1`](../tools/package-bundle.ps1) 能把**选定的组件**（连同已经下好的模型，可选；连同
装环境要用的 Python 依赖库 wheel，也可选——两者都带上，目标机器整个装起来的过程可以完全不用联网）打成一个
压缩包；在新机器上解压、双击里面的「安装并接入如意.cmd」，会给每个组件建好虚拟环境、装好依赖，然后：
带了模型就直接离线登记，没带就跑该组件自己的 `download-model.ps1`（联网下）。用法见
[`tools/README.md`](../tools/README.md)；本节是**给组件作者**的——你的组件要满足什么，才会被这个工具正确发现、
正确打包。

### 6.1 自动发现的最低要求（不满足就不会出现在候选列表里）

1. 组件目录在仓库根目录下一层，或者 `mcp/` 下一层（`mcp/<name>/`）——跟 §1 的「一个组件一个顶层目录」一致，
   打包器只多认一层嵌套。
2. 目录里有 `pyproject.toml` 和 `scripts\install.ps1` 这两个文件（路径、大小写都要对）。
3. `install.ps1` **幂等**、**只用相对自己的路径**（`$here = Split-Path -Parent $MyInvocation.MyCommand.Path`，
   `$root = Split-Path -Parent $here`，venv 建在 `$root\.venv`）——本仓两个组件的 `install.ps1` 已经是这个形状，
   照抄就对。打包器生成的 `setup.ps1` 在**任意解压位置**调用它，`install.ps1` 自己算出的路径必须跟着解压位置走，
   不能硬编码打包时的路径。
4. 组件的 Python 包目录名形如 `ruyi_<name>/` 且里面有 `__main__.py`——打包器用这个规律猜模块名
   （给 `python -m <module> register` 用）。两个现有组件（`ruyi_asr_shim`／`ruyi_asr_stream`）都是这样。

### 6.2 `pyproject.toml` 必须显式列出包（不是可选项）

```toml
[tool.setuptools]
packages = ["ruyi_你的组件名"]
```

**没有这一行，打包已下模型时会直接炸**：模型目录（`models/`）会被打包器当成源码的兄弟目录一起放进组件文件夹，
`uv pip install -e .` 触发 setuptools 的自动包发现，会把 `models/` 也当成一个「顶层包」候选，报
`Multiple top-level packages discovered in a flat-layout`，`install.ps1` 直接失败。两个现有组件都已经这样写；
新组件照抄，不要指望「反正我不会打包模型」——你不知道以后谁会想打包。

### 6.3 模型目录的约定（想要「已下模型可选打包」才需要）

- 装模型的地方固定叫 `models/`，紧贴组件根目录（跟 `ruyi_<name>/` 同级）。
- 每一份可选的模型（不同体量、不同变体）各自一个子目录，目录名就是它的标识（比如 `Qwen3-ASR-0.6B-hf`、
  `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`）——打包器按子目录名列出候选，不看目录内容。
- 没有 `models/` 目录也完全没问题：打包器会把该组件归成「没有模型概念」，只打包源码，目标机器直接
  `register`（无参）。多数 MCP 类组件大概率是这种。

### 6.4 想要「打包时带已下模型 → 目标机器离线登记」，还要做一件事

打包器不会瞎猜你的 `register` 该传什么参数（`--model-dir`？`--models-root`？两个都要？）——这件事登记在
`tools/package-bundle.ps1` 顶部的 `$script:ComponentOverrides` 表里，你的组件 id 不在表里也能打包（走一条
通用兜底：一份模型选 `--model-dir`，多份选 `--models-root`，能凑合但不一定对），**想要打包器精确拼出你组件
认得的参数**，在那张表里加一条：

```powershell
"你的组件id" = @{
    Name      = "给人看的名字"
    ModelKind = "sizes"   # 或 "pair"（流式+离线两个独立槽位，参考 asr-stream 那条）
    SizeChoices = @(
        @{ Key = "触发 --model 用的名字"; Dirname = "models 下的子目录名"; Label = "GUI 里显示的说明" }
    )
    BuildRegisterArgs = {
        param($Selection)   # 这次选中了哪些
        if (-not $Selection -or $Selection.Count -eq 0) { return $null }   # 没选 = 没带模型
        @("register", "--你的组件认的参数名", (Join-Path "{CompDir}" "models"))   # {CompDir} 别改——目标机器现算真实路径
    }
}
```

`{CompDir}` 是唯一的占位符，`setup.ps1` 在目标机器上把它换成这个组件解压后的真实绝对路径——别的位置不要
硬编码任何路径（打包时的路径在目标机器上没有意义）。

### 6.5 打包器不管、组件自己已经管好的事

- 打包器**只搬源码 + 你选中的模型子目录**，不碰 `.venv*`（任意后缀）、`__pycache__`、`*.egg-info`、
  `.pytest_cache` 这类构建产物——目标机器上的虚拟环境永远是 `setup.ps1` 现建的（venv 跨机器不可移植，见
  `tools/README.md` 的说明），不用担心它们被误打包进去。
- `tests/`、`samples/` 缺省不打包（开发用，不影响组件运行）；真要带上可以在打包器界面勾「包含测试代码」。

### 6.6 想要「打包依赖库 → 目标机器装环境不用联网」，`pyproject.toml` 写对就够（多数组件不用改脚本）

**机制：本机现成的优先，缺的才下载。** 打包器不会去联网重新解析一遍依赖，而是：

1. 用**你组件自己 `.venv` 的 python** 跑 [`tools/localwheels.py`](../tools/localwheels.py)，把本机已装的包
   （`importlib.metadata` 的 RECORD）**还原成 wheel 文件**——本机装好、跑通过的那一套，一个都不用再下，
   轮子的平台标签（`cp312-…-win_amd64`）原样保留。所以组件要先跑过一次 `install.ps1`（有 `.venv`）。
2. 用 `pyproject.toml` 里的 `[project] dependencies` 与 `[build-system] requires`（`Get-PyProjectDeps` 用正则抽，
   只要是「每项一个带引号的字符串」的平常写法就行）逐条问：本机满不满足？**不满足的才去下**（典型例子：
   本机 venv 里没有的 build 依赖 `setuptools`）。GUI 里勾上「打包依赖库」就会显示「本机已有 N 个包…；需要联网
   下载：…」，命令行模式则打进日志。
3. 下载用系统 pip，但**按组件 `install.ps1` 里写的 Python 版本挑轮子**（`--python-version 3.12 --platform
   win_amd64 --only-binary=:all:`），**不是**按运行打包器的那个系统 Python 挑——否则系统是 3.13 时下出来的
   全是 `cp313` 轮子，3.12 的 venv 离线装不上。
4. **离线自检**：在一个全新的临时 Python 环境里、用**空的 uv 缓存**、按目标机器 `setup.ps1` 的办法
   （`UV_OFFLINE=1` + `UV_FIND_LINKS`）先 dry-run 凑依赖、再真装一次项目本体。空缓存是关键——否则在装过一堆
   东西的机器上，缺的包会被 uv 缓存悄悄补上，自检永远通过。不通过就中止打包并说清楚缺什么。

**例外：装依赖时按显卡自己挑构建的包**（比如 asr-shim 的 `install.ps1` 按显卡挑 CPU／CUDA／ROCm 版 PyTorch，
这种包不会明写在 `dependencies` 里）——要在 `$script:ComponentOverrides` 你组件那条里加 `GpuOrder`／`GpuTorch`
两项，打包器才知道有哪几种构建可选：

```powershell
GpuOrder = @("nvidia", "amd", "cpu")     # 界面下拉框里的先后顺序（Hashtable 自己不保序）
GpuTorch = @{
    nvidia = @{ Label = "GUI 下拉框里显示的说明"; IndexUrl = "https://download.pytorch.org/whl/cu128"; Approx = "约 3 GB" }
    cpu    = @{ Label = "……"; IndexUrl = "https://download.pytorch.org/whl/cpu"; Approx = "约 200 MB" }
    amd    = @{ Label = "……"; Rocm = $true; Approx = "数 GB" }   # 直链型：没有 index，见下
}
```

- **本机 venv 里装的是哪种（看 torch 版本号里的 `+cu128`／`+cpu`／`+rocm`），选它就不用下**；选别的才下——
  `IndexUrl` 型走 PyTorch 的 index，只下 torch 本体（它的依赖本机已有）。
- **`Rocm = $true`（直链型）**：AMD 的 ROCm 是几个写死的直链，不是 PyPI 兼容的 index，`uv` 的
  `--offline`／`--find-links` 管不到。打包器把它们用 `curl` 下到 `.offline-wheels\rocm\`，`install.ps1` 的 AMD
  分支见到这个目录就直接装本地文件。版本号要和 `install.ps1` 里的一致——放在 `$script:ComponentOverrides`
  的 `Rocm` 表里，打包时会核对，对不上就报错。
- 打进 manifest 的 `gpuVariant` 会让 `setup.ps1` 给 `install.ps1` 传 `-Gpu <变体>`：包里带的是哪种 PyTorch 就
  装哪种，不让 `install.ps1` 按**目标机器**的显卡再猜一遍（带 NVIDIA 版、目标机是 AMD 卡，它会转去联网下
  ROCm，离线必然失败）。

没有 `GpuTorch` 表的组件，GUI 上「打包依赖库」只是个开关；有的话（目前只有 asr-shim）会多一个显卡类型
下拉框，每一项标着「本机已装，直接用」或「需下载」；`-OfflineDeps` 对应给 `nvidia`／`amd`／`cpu` 而不是随便
一个非空值（给了别的值会报错，不再悄悄跳过 PyTorch）。

> **⚠ AMD 这条路未经真机验证**（开发机只有英伟达卡）：下载、目录约定、`install.ps1` 的本地文件分支都按 AMD
> 官方文档与现有在线分支的写法对了一遍，离线自检也会 dry-run 它，但没有在真的 AMD 卡上装过、跑过。
