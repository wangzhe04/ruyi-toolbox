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

## 5. 边界情况怎么算（2026-09-21 联调后补）

- **文件名与 `id` 不一致**：如意当作没装（不猜哪个对）。组件的 `register` 必须保证两者一致。
- **登记还在、东西没了**（整个目录被挪走／改名、虚拟环境被删）：`run.command`／`run.cwd` 不存在 → 如意当作没装并记一条日志，
  不会崩、也不会去执行别的东西。修法是在新位置重跑组件的 `register`。
- **自动生成的服务商会出现在如意的服务商列表里**：只提供 `asr` 的组件没有对话接口，被选去对话会得到一个 404。
  这是如意一侧待办（让只做语音的条目不进对话模型候选），组件不用为此做任何事。
- **Windows 上 venv 的 `python.exe` 是转发壳**：进程表里会有两个 python 进程。如意回收时杀整棵树；组件自己的父进程看门狗是第二道保险。
