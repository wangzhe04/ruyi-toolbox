# tools/ —— 打包与迁移

装好的 ruyi-toolbox 组件要挪去另一台机器（重装系统、换电脑、给别人一份能直接用的），
不用把 `install.ps1`／`download-model.ps1` 每个组件挨个敲一遍：`package-bundle.ps1` 把你选定的组件
（连同已经下好的模型，可选；连同装环境要用的 Python 依赖库，也可选）打成一个压缩包；在新机器上解压、
双击里面的「安装并接入如意.cmd」就好。模型和依赖库都带上的话，目标机器整个装起来的过程完全不用联网——
适合要装到不能访问互联网的机子上的场景，见下面「离线依赖库」。

## 在这台机器上打包

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\package-bundle.ps1
```

不带参数直接跑会弹一个窗口：每个组件一块，勾选要不要打包它、要不要顺带打包已经下好的模型（体量大的
组件——比如本地语音识别——能省掉在新机器上重新下几个 GB 的模型），下面还有一个独立的「打包依赖库（完全
离线安装）」勾选（见下面「离线依赖库」；带 GPU 版本选择的组件——目前只有 asr-shim——勾了它还会多出一个
挑 CPU／NVIDIA 版 PyTorch 的下拉框）；再下面选输出目录和压缩包名，点「开始打包」，日志实时滚动，完成后
能直接「打开输出目录」。也可以双击 `打包扩展组件.cmd`，效果一样。

脚本/CI 用（不开窗口）：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\package-bundle.ps1 `
    -Components asr-shim,asr-stream `
    -Models "asr-shim=qwen3-asr-0.6b;asr-stream=streaming:sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20,offline:sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17" `
    -OfflineDeps "asr-shim=cpu;asr-stream=1" `
    -OutputDir dist -ZipName my-bundle
```

- `-Components`：逗号分隔的组件 id（不给就报错列出能打包的有哪些；根目录 `ls` 一下也能看到）。
- `-Models`：可选。每个组件一段、分号隔开：
  - 「多体量」型组件（比如 asr-shim）：`组件id=体量key1,体量key2`（逗号分隔，能选多个——`auto` 模式会把
    每份都列出来让用户在如意里挑）。
  - 「流式+离线两槽位」型组件（比如 asr-stream）：`组件id=streaming:目录名,offline:目录名`（哪个槽位不给
    就是不带那份模型）。
  - 不给某个组件的 `-Models` 段＝不带模型，目标机器会自动联网下载（跟全新安装一样）。
- `-OfflineDeps`：可选，见下面「离线依赖库」一节。每个组件一段、分号隔开，`组件id=值`：没有 GPU 版本
  选择的组件随便给个非空值（比如 `1`）就行；有 GPU 版本选择的组件（目前只有 asr-shim）给 `nvidia`、`amd`
  或 `cpu`（本机 venv 里装的是哪种，那种就不用下载）。不给某个组件的 `-OfflineDeps` 段＝不打包它的依赖库，目标机器装环境这步仍然要联网。
- `-IncludeTests`：把 `tests/` 也打包进去（一般不需要）。
- `-KeepStagingDir`：压完之后把解压好的那份目录也留在输出目录里，方便打包前先自己看一眼。

打包这一步只在**这台**机器上跑；产物是自包含的一个 `.zip`，目标机器不需要装 ruyi-toolbox 仓库本身。

## 在新机器上装

1. 解压这个压缩包到任意目录（不要放进如意工作台自己的安装目录里；**也别放进嵌套很深的文件夹**——见下面
   「已知限制」）。
2. 双击「安装并接入如意.cmd」。第一次可能被 Windows SmartScreen 拦一下，点「更多信息 → 仍要运行」。
3. 等它跑完：每个组件会现建虚拟环境、装依赖（打包时勾了「打包依赖库」的组件这一步不联网，直接用带着的
   wheel 文件；没勾的组件跟手动跑 `install.ps1` 一样要联网下载）；带了模型的组件直接离线登记，没带模型的
   组件会跑该组件自己的 `download-model.ps1`（联网下模型再登记）。
4. 重启如意工作台（或第一次启动），设置页「MCP／扩展组件」栏应该能看到新组件了。

`setup.ps1`（就是那个 `.cmd` 调的脚本）也能自己手动跑，支持 `-Only`／`-Skip` 只处理/跳过某几个组件id，
`-InstallUv` 在机器上没有 `uv` 时自动装：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Only asr-shim -InstallUv
```

## 为什么不直接把 `.venv` 打包进去

Windows 上 venv 的 `pyvenv.cfg` 记着建它那台机器上 Python 解释器的绝对路径；换台机器这条路径多半不存在，
venv 就起不来（更别说 GPU 构建的 PyTorch 认死了那台机器的显卡型号）。唯一稳的路子是只打包**源码**，
在新机器上用当地的 `uv` 现建 venv——好在 `uv` 装依赖本来就快，多数组件几十秒到几分钟就能装完
（重的可能要装 PyTorch，那部分本来在任何一台新机器上首次安装都要下）。

## 离线依赖库

「打包依赖库」（GUI 勾选，或 CLI `-OfflineDeps`）解决的是另一半问题：模型打包进去了，但每个组件自己的
Python 依赖（`pyproject.toml` 里 `dependencies`／`[build-system] requires` 列的那些——torch、
transformers、numpy、sherpa-onnx 之类）默认还是 `install.ps1` 在目标机器上现场用 `uv pip install` 联网装。
勾了这个选项，打包器会把这些依赖（含 asr-shim 那种按显卡挑的 PyTorch）的 wheel 文件，连同一份 `uv.exe`，
一起放进压缩包；`setup.ps1` 在目标机器上看到组件目录下有 `.offline-wheels/` 就会把 `UV_OFFLINE=1`／
`UV_FIND_LINKS=<那个目录>` 这两个环境变量设好再调 `install.ps1`——它调用的裸 `uv` 自己认这两个变量，不出网、
只认本地这份 wheel。

**本机现成的优先，缺的才下载。** 这些 wheel 怎么来的：

- **本机 venv 里已装的包，直接还原成 wheel 文件**（[`localwheels.py`](localwheels.py)，只用标准库，用组件自己
  `.venv` 的 python 跑）——本机装好、跑通过的那一套，一个都不用重下，平台标签原样保留。asr-shim 的 CUDA 版
  torch（近 3 GB）就是这么来的，几分钟内还原完，不受网络影响。所以**组件要先跑过一次 `install.ps1`**。
- **本机没有的才下载**：`pyproject.toml` 里每条要求逐一问本机满不满足，不满足的（比如本机 venv 里没装的
  build 依赖 `setuptools`）才用 pip 去下。**轮子按组件 `install.ps1` 里写的 Python（3.12）挑**，不是按运行打包器
  的系统 Python——系统是 3.13 时，直接 `pip download` 下出来的全是 `cp313` 轮子，3.12 的 venv 装不上。
- **界面上会告诉你**缺哪个：勾上「打包依赖库」就显示「本机已有 N 个包……；需要联网下载：……」，asr-shim 的
  显卡下拉框里每一项都标着「本机已装，直接用」或「需下载」。选 **NVIDIA** 就是本机现成的；**AMD**（ROCm）和
  **CPU** 是本机没有的构建，选了才会去下（AMD 走 AMD 官方直链，⚠ 未经真机验证）。
- **自检**：打完轮子，会在一个全新的临时环境里、用**空的 uv 缓存**、按目标机器的办法（`UV_OFFLINE`＋
  `UV_FIND_LINKS`）演练一遍装环境；缺轮子就中止并说清楚。空缓存是关键——否则在这台装过一堆东西的机器上，缺的
  包会被 uv 缓存悄悄补上，自检永远通过。
- 每个外部命令都有总时限，输出实时打进日志，30 秒没动静就报一次「还在跑」；超时会杀掉并说明多半是网络／代理
  卡住了。以前直接 `pip download --quiet`，代理一挂住就是无限期卡在「打包依赖库……」那一行、界面还整个不响应。

也测过用一个连不通的代理、空的 uv 缓存、只用包里那份 `uv.exe`，在解压出来的目录里跑组件的 `install.ps1`，
5 秒装出完整环境，全程不联网。

模型 + 依赖库都打包的话，目标机器只需要预先装好 Python 3.12（`uv venv --python 3.12` 认的那个解释器；
离线模式下 `uv` 不会去联网下载解释器本身），装完 Python 之后整套流程——解压、装环境、登记模型、启动
服务——都不用联网。已知限制（AMD 未验证、Python 3.12 前提）见下面「已知限制」。

## 进度、取消与预检

**打包时的进度**（界面与命令行都有）：

- 界面：进度条 ＋「百分比 · 当前阶段 · 细节」（如 `84%  压缩  model.int8.onnx  215 MB / 487 MB`）＋「已用／预计剩余」。
  命令行：`Write-Progress` 画进度条，并且每前进 5% 打一行 `[ 45%] 阶段  已用 0:07，约剩 0:03`——输出重定向到文件时
  看不到进度条，这几行就是给这种场合的。
- 总进度按**估算耗时**给各阶段分权重（复制模型、还原轮子、下载、自检、压缩），而不是按字节：各阶段速度差得远
  （复制几百 MB/s、还原要压缩只有几十 MB/s、走代理下载才几 MB/s），按字节算进度条会在下载那段停很久、其它段一闪而过。
  **阶段内部的比例全是实测的**：复制看目标目录长了多少字节；还原轮子由 `localwheels.py` 每 ~0.4 秒汇报
  `PROG 已写 总量`；下载读 pip（`--progress-bar raw`）与 curl 的进度行；压缩按已写入的字节数。剩余时间＝已用时间 ÷
  已完成比例 × 剩余比例，拿实测速度校准（进度不到 3% 时不给，免得乱报）。
- **目标机器上装环境**：`setup.ps1` 有「安装扩展组件」总进度条（`[第几个/共几个]`），各组件的 `install.ps1` 每一步都标
  `[第几步/共几步]` 并挂一条进度条；联网装的时候 uv 自己的下载进度条照常显示。

**取消**：界面上有「取消打包」按钮；打包中点窗口右上角的 × 也是先取消、收拾干净后再关（不会直接把后台还在写的文件扔下不管）。
下载、还原、复制、压缩这些长循环每个检查点都看取消标志，命中后杀掉子进程（连同它的子进程树）、删掉暂存目录和半截的
`.zip.part`。

**预检**（动手之前就把注定失败的情况拦下来，并且说清楚怎么办）：

- **磁盘空间**：暂存目录（`%TEMP%`）与输出目录各要多少，同一个盘就加起来算；不够直接报「磁盘空间不够：…约需 X，只剩 Y」。
- **网络**：需要下载的话，先用 curl 探一下要用的站点（PyPI／PyTorch／AMD），连不上几秒内就报，并写出当前代理——而不是像
  以前那样让 pip 走一个挂住的代理、什么都不说地卡十几分钟。选「本机已装」的显卡类型就不需要联网。
- 输出目录能不能写、压缩包名里有没有非法字符。

**卡死检测与校验**：pip 240 秒、curl 180 秒没有任何输出就当卡死、杀掉并报错；外部命令另有总时限；30 秒没动静会打一行
「还在跑」。zip **先写成 `.zip.part`**（直接写在输出目录里，不再先写 `%TEMP%` 再跨盘搬），写完后核对 zip 里的**文件数和
总字节数**与暂存目录一致，通过了才改名成 `.zip`——所以中途失败、被取消都不会留下一个看着像样的残缺包，也不会覆盖掉旧包。
失败信息会翻成人话（磁盘满、路径太长、文件被占用……），不再一律说「多半是路径太长」。

## 测试

```powershell
python -m unittest discover -s tools/tests
```

含两部分：`localwheels.py` 的单测（版本比较、轮子标签、还原后的内容与 RECORD 哈希、字节进度），以及对所有 `.ps1`
的体检（语法、UTF-8 BOM＋CRLF、没有孤立回车符）与 `progress_tests.ps1`（进度模型、外部命令的超时／卡死／取消、复制、压缩
与校验、预检）。不联网、不碰真模型。

## 给新组件的规范

`package-bundle.ps1` 靠约定自动发现组件（根目录下一层，或 `mcp/` 下一层，有 `pyproject.toml` +
`scripts\install.ps1` 就行），不用改这个脚本。完整清单——包括「不改脚本能自动打包，但想要更精确的模型
参数还要改一处小表」——见 [`../docs/00-component-registry.md` §6](../docs/00-component-registry.md#6-打包与迁移tools\package-bundle.ps12026-09-21-起)。

## 已知限制

- **路径太长**：Windows PowerShell 5.1（本脚本跑在它上面）对超过 260 字符的完整路径不友好。模型文件动辄
  上百个、文件名本来就长，打包／解压时如果放的目录本身也很深（尤其是 OneDrive 同步的 `文档` 目录那种
  `C:\Users\<name>\OneDrive - <公司名>\文档\...`），偶尔会撞上这个上限。打包这一步已经做了长路径规避
  （组装／压缩都发生在系统临时目录下的短路径，只把最终那一个 `.zip` 文件挪到你要的输出目录），**解压**这
  一步不受本脚本控制（是 Windows 自己的解压逻辑），撞上了就换个浅一点的目录解压（比如直接 `C:\` 根目录下），
  或者到系统设置搜「启用 Win32 长路径」打开。
- **`uv` 是硬依赖**：目标机器上没有它，`setup.ps1` 会告诉你怎么装，或者加 `-InstallUv` 自动装
  （需要联网从 astral.sh 拉安装脚本；带了离线依赖库的组件会顺带打包一份 `uv.exe`，见上面「离线依赖库」，
  这种情况下目标机器不用先自己装 uv，`-InstallUv` 也用不上）。
- **AMD ROCm 的离线打包未经真机验证**：asr-shim 的 `install.ps1` 装 ROCm 版 PyTorch 用的是几个写死的 wheel
  直链而不是 PyPI 兼容的 index，`uv` 的 `--offline`／`--find-links` 管不到它们。打包器把这几个文件用 `curl` 下到
  `.offline-wheels\rocm\`，`install.ps1` 的 AMD 分支见到这个目录就直接装本地文件；离线自检也会 dry-run 它。
  但这条路**没有在真的 AMD 卡上装过、跑过**（开发机只有英伟达卡），和 `install.ps1` 里原有的在线 ROCm 分支一样
  是「按 AMD 官方文档抄对了」的状态。
- **单个文件超过 2 GB 没问题了**：以前压缩用 `Compress-Archive`，它在 Windows PowerShell 5.1 里遇到单个文件
  超过 2 GB 就报 `Stream was too long`（实测 2.3 GB 的文件必挂），而 Qwen3-ASR-1.7B 的 `model.safetensors` 有
  4 GB、CUDA 版 PyTorch 的轮子近 3 GB。现在改成直接用 .NET 的 `ZipArchive` 流式写；轮子、权重这类已经压过的
  东西用「仅存储」，不白费时间再压一遍。
- **离线依赖库不含 Python 解释器本身**：目标机器要预先装好 Python 3.12（`uv venv --python 3.12` 找的
  就是它），离线模式下 `uv` 不会去联网下载解释器。
