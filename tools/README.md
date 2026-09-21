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
  选择的组件随便给个非空值（比如 `1`）就行；有 GPU 版本选择的组件（目前只有 asr-shim）给 `cpu` 或
  `nvidia`。不给某个组件的 `-OfflineDeps` 段＝不打包它的依赖库，目标机器装环境这步仍然要联网。
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
勾了这个选项，打包这一步会用这台机器上的 `pip download` 把这些依赖（含 asr-shim 那种按 GPU 型号单独挑的
PyTorch）的 wheel 文件都拉下来，连同一份 `uv.exe`，一起放进压缩包；`setup.ps1` 在目标机器上看到组件目录
下有 `.offline-wheels/` 就会把 `UV_OFFLINE=1`／`UV_FIND_LINKS=<那个目录>` 这两个环境变量设好再调
`install.ps1`——`install.ps1` 本身一个字不改，它调用的裸 `uv` 自己认这两个变量，不出网、只认本地这份
wheel。也测过用一个连不通的代理强制断网重跑一遍整条链路（打包→解压→装环境→登记→起服务→`/health`
返回 200），全程没有失败，证明这条路径确实不依赖网络。

模型 + 依赖库都打包的话，目标机器只需要预先装好 Python 3.12（`uv venv --python 3.12` 认的那个解释器；
离线模式下 `uv` 不会去联网下载解释器本身），装完 Python 之后整套流程——解压、装环境、登记模型、启动
服务——都不用联网。已知限制（AMD ROCm、Python 3.12 前提）见下面「已知限制」。

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
- **AMD ROCm 不支持离线打包**：asr-shim 的 `install.ps1` 装 ROCm 版 PyTorch 用的是几个写死的 wheel 直链
  而不是像 CPU／NVIDIA 那样走 PyPI 兼容的 index URL，跟 `--find-links`／`--offline` 这套机制对不上；
  ROCm 本来就是 `install.ps1` 自己注释里写着「未验证」的路子，这里就不勉强支持了——勾了「打包依赖库」的
  asr-shim 只有 CPU／NVIDIA 两个 GPU 变体可选，装 ROCm 版的机器请让 `install.ps1` 正常联网装。
- **离线依赖库不含 Python 解释器本身**：目标机器要预先装好 Python 3.12（`uv venv --python 3.12` 找的
  就是它），离线模式下 `uv` 不会去联网下载解释器。
