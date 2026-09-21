#Requires -Version 5.1
<#
.SYNOPSIS
  把 ruyi-toolbox 里选定的组件（连同已经下好的模型，可选）打成一个压缩包，在另一台电脑上解压、
  双击一下就能被如意工作台自动发现并拉起 —— 不用再一个个敲 install.ps1 / download-model.ps1。

.DESCRIPTION
  两种用法：
    不带 -Components 直接跑 —— 弹出一个窗口，勾组件、勾要不要带上已下的模型、选输出目录，点「开始打包」。
    带 -Components 跑（脚本/CI 用）—— 不开窗口，直接打包，退出码 0 = 全部组件都处理成功。

  产物是一个 .zip：里面每个组件一个子目录（源码 + scripts，不含 .venv/__pycache__/*.egg-info，
  模型按你的勾选决定带不带），外加一份 setup.ps1（真正干活的）和一份「安装并接入如意.cmd」
  （双击它 = 跑 setup.ps1）。在新电脑上解压到任意目录、双击那个 .cmd：它会给每个组件建 venv、
  装依赖（这一步仍然要联网，跟你现在手动跑 install.ps1 一样），然后：
    · 这次打包带了模型 —— 直接向如意登记（不再触网下模型）；
    · 没带模型 —— 跑该组件自己的 download-model.ps1（联网下模型再登记，跟全新安装一样）。

  为什么不直接把 .venv 一起打包：Windows 的 venv 里 pyvenv.cfg 记的是建它那台机器上 Python 的
  绝对路径，换台机器多半连不上（更别说 GPU 构建的 torch 认死了那台机器的显卡）。只打包源码、
  在新机器上用当地的 uv 重新建 venv，这是唯一稳的路子——好在 uv 装依赖本来就快。

  新增组件不用改这个脚本：根目录下任意一个子目录（或 mcp/ 下一层）只要有 pyproject.toml 和
  scripts\install.ps1，就会被自动发现、列进候选。模型该怎么打包／怎么注册这类组件专属的细节，
  住在 $script:ComponentOverrides 这张表里；不认识的新组件会走一条通用兜底（能用但不如手写的准）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\tools\package-bundle.ps1
  # 弹窗口，勾选后点「开始打包」。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\tools\package-bundle.ps1 -Components asr-shim,asr-stream -Models "asr-shim=0.6b;asr-stream=streaming:sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20,offline:sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17" -OutputDir dist -ZipName my-bundle
  # 不开窗口，直接打包（脚本/CI 用；-Models 留空＝不带模型，走目标机器联网下载那条路）。
#>
[CmdletBinding()]
param(
    # 给了这个就是命令行模式（不开窗口）；不给就弹窗口。
    [string[]]$Components,
    # 命令行模式专用：每个组件一段，分号隔开。形状见上面 EXAMPLE 与 tools/README.md。
    [string]$Models = "",
    # 命令行模式专用：组件id=值，分号隔开。没有 GpuTorch 表的组件随便给个非空值（比如 1）；
    # 有 GpuTorch 表的组件（目前只有 asr-shim）给 GPU 变体 key："cpu" 或 "nvidia"。
    [string]$OfflineDeps = "",
    [string]$OutputDir = "",
    [string]$ZipName = "",
    [switch]$IncludeTests,
    [switch]$KeepStagingDir,
    # 测试专用改道口子：指向一个假的 toolbox 根目录，不碰真仓库。正常使用不要给这个参数。
    [string]$Root = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $Root) { $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
$Root = (Resolve-Path -LiteralPath $Root).Path

# powershell -File 这种方式调本脚本时（本仓所有脚本都这么用），未加引号的 -Components a,b 会被当成一整个字符串传进来
# （逗号不会自动拆数组），只有直接用 PowerShell 自己的语法调用（& .\xxx.ps1 -Components a,b）才会正常拆。
# 两种调法都要支持，所以这里对每个元素再按逗号拆一次（真数组进来也不受影响，元素里本来就没逗号）。
if ($Components) { $Components = @($Components | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }

# ── 组件专属细节：模型怎么分组呈现、register 命令怎么拼 ──────────────────────────────────
# 新组件不在这张表里也能打包（走下面 Get-ToolboxComponents 里的通用兜底），只是没这么精细。
# BuildRegisterArgs 收到的是「这次选了哪些模型」，回一个 string[]（register 子命令 + 参数，
# 目标路径写成 {CompDir} 占位——setup.ps1 在目标机器上跑的时候才替换成真实绝对路径）或 $null
# （$null＝这次没带模型，setup.ps1 改跑该组件自己的 download-model.ps1）。
$script:ComponentOverrides = @{
    "asr-shim" = @{
        Name      = "本地语音识别（Qwen3-ASR）"
        ModelKind = "sizes"
        # 顺序＝GUI 里的呈现顺序；Key 要跟 asr-shim 的 --model 认的名字一致。
        SizeChoices = @(
            @{ Key = "qwen3-asr-0.6b"; Dirname = "Qwen3-ASR-0.6B-hf"; Label = "0.6B（省显存，约 2 GB）" }
            @{ Key = "qwen3-asr-1.7b"; Dirname = "Qwen3-ASR-1.7B-hf"; Label = "1.7B（更准，约 5 GB 显存）" }
        )
        BuildRegisterArgs = {
            param($Selection)   # $Selection: string[]，选中的 Key（未必用到——auto 模式是扫整个 models 目录）
            if (-not $Selection -or $Selection.Count -eq 0) { return $null }
            @("register", "--model", "auto", "--models-root", (Join-Path "{CompDir}" "models"))
        }
        # torch 不在 pyproject.toml 的 dependencies 里（install.ps1 自己按显卡挑索引装）——打包离线依赖时
        # 单独按这张表再下一次。ROCm 用的是固定 wheel 直链而不是 index（见 install.ps1），这里先不支持，
        # 见 tools/README.md「已知限制」。
        GpuTorch = @{
            cpu    = @{ Label = "CPU（体积小，约 200 MB，推理慢）"; IndexUrl = "https://download.pytorch.org/whl/cpu" }
            nvidia = @{ Label = "NVIDIA（CUDA cu128，约 3 GB）"; IndexUrl = "https://download.pytorch.org/whl/cu128" }
        }
    }
    "asr-stream" = @{
        Name      = "本地实时语音识别（流式，sherpa-onnx）"
        ModelKind = "pair"
        # 目录名匹配这个正则的算「离线整句识别（SenseVoice 那一路）」候选，其余算「流式」候选。
        OfflinePattern = "sense.?voice"
        BuildRegisterArgs = {
            param($Selection)   # $Selection: @{ Streaming = <目录名或空>; Offline = <目录名或空> }
            $args = @("register")
            $any = $false
            if ($Selection.Streaming) { $args += @("--model-dir", (Join-Path "{CompDir}" (Join-Path "models" $Selection.Streaming))); $any = $true }
            if ($Selection.Offline)   { $args += @("--offline-model-dir", (Join-Path "{CompDir}" (Join-Path "models" $Selection.Offline))); $any = $true }
            if (-not $any) { return $null }
            $args
        }
    }
}

# 简单正则抽取 pyproject.toml 里 [project] 的 dependencies 与 [build-system] 的 requires——不引入 TOML
# 解析器，两个现有组件的写法（单行或多行数组，每项一个带引号的字符串）都是这个形状。负向前瞻是防止
# 「optional-dependencies」这种以 dependencies 结尾的别的键被误当成主依赖数组。
function Get-PyProjectDeps {
    param([string]$PyProjectPath)
    if (-not (Test-Path -LiteralPath $PyProjectPath)) { return @{ Deps = @(); BuildDeps = @() } }
    $text = Get-Content -LiteralPath $PyProjectPath -Raw
    function Get-QuotedArrayAfter($pattern) {
        $m = [regex]::Match($text, $pattern, [System.Text.RegularExpressions.RegexOptions]::Singleline)
        if (-not $m.Success) { return @() }
        return ,@([regex]::Matches($m.Groups[1].Value, '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value })
    }
    $deps = Get-QuotedArrayAfter '(?<![-\w])dependencies\s*=\s*\[(.*?)\]'
    $buildDeps = Get-QuotedArrayAfter 'requires\s*=\s*\[(.*?)\]'
    return @{ Deps = $deps; BuildDeps = $buildDeps }
}

function Find-PipExe {
    $cmd = Get-Command pip -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Find-PythonExe {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# 打包离线依赖用：把 wheel 文件（不装、只下）拉到本地目录，供目标机器上 uv 的 --offline + --find-links
# 用。故意用系统的 pip 而不是 uv——这版 uv（0.11.21）的 `uv pip` 底下没有单独的「只下载不装」子命令；
# pip download 是这件事的标准做法，跟 uv 装的时候看的是同一套 PyPI/index，wheel 文件本身通用。
function Invoke-PipDownload {
    param([string[]]$Specs, [string]$DestDir, [string]$IndexUrl = "")
    if (-not $Specs -or $Specs.Count -eq 0) { return }
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    $extra = @()
    if ($IndexUrl) { $extra += @("--index-url", $IndexUrl) }
    $pip = Find-PipExe
    if ($pip) {
        & $pip download @Specs -d $DestDir @extra --quiet
    } else {
        $py = Find-PythonExe
        if (-not $py) { throw "打包依赖库要用 pip 下载 wheel 文件——这台机器上找不到 pip 也找不到 python。" }
        & $py -m pip download @Specs -d $DestDir @extra --quiet
    }
    if ($LASTEXITCODE -ne 0) { throw ("下载依赖库失败（pip download 退出码 " + $LASTEXITCODE + "）：" + ($Specs -join ", ")) }
}

# ── 发现候选组件：根目录下一层 ＋ mcp/ 下一层，凡是有 pyproject.toml + scripts\install.ps1 的 ──
function Get-ToolboxComponents {
    param([Parameter(Mandatory)][string]$RepoRoot)

    $reserved = @("docs", "tools", ".git", "dist")
    $candidates = New-Object System.Collections.Generic.List[System.IO.DirectoryInfo]
    Get-ChildItem -LiteralPath $RepoRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -notin $reserved -and $_.Name -ne "mcp" } | ForEach-Object { $candidates.Add($_) }
    $mcpDir = Join-Path $RepoRoot "mcp"
    if (Test-Path -LiteralPath $mcpDir) {
        Get-ChildItem -LiteralPath $mcpDir -Directory -ErrorAction SilentlyContinue | ForEach-Object { $candidates.Add($_) }
    }

    $out = @()
    foreach ($d in $candidates) {
        $pyproj = Join-Path $d.FullName "pyproject.toml"
        $install = Join-Path $d.FullName "scripts\install.ps1"
        if (-not (Test-Path -LiteralPath $pyproj) -or -not (Test-Path -LiteralPath $install)) { continue }

        $relId = ($d.FullName.Substring($RepoRoot.Length)).TrimStart('\', '/') -replace '\\', '/'
        $moduleDir = Get-ChildItem -LiteralPath $d.FullName -Directory -Filter "ruyi_*" -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "__main__.py") } | Select-Object -First 1
        $module = if ($moduleDir) { $moduleDir.Name } else { "" }

        $override = $script:ComponentOverrides[$relId]
        $modelsDir = Join-Path $d.FullName "models"
        $hasModelsDir = Test-Path -LiteralPath $modelsDir

        $comp = [ordered]@{
            Id                = $relId
            Name              = if ($override) { $override.Name } else { $relId }
            Dir               = $d.FullName
            Module            = $module
            DownloadScriptRel = if (Test-Path -LiteralPath (Join-Path $d.FullName "scripts\download-model.ps1")) { "scripts\download-model.ps1" } else { "" }
            ModelKind         = "none"
            SizeChoices       = @()
            StreamingChoices  = @()
            OfflineChoices    = @()
            BuildRegisterArgs = { param($Selection) return $null }
            PyDeps            = @()
            BuildDeps         = @()
            GpuTorch          = if ($override -and $override.ContainsKey('GpuTorch')) { $override.GpuTorch } else { $null }
        }
        $pd = Get-PyProjectDeps -PyProjectPath $pyproj
        $comp.PyDeps = $pd.Deps
        $comp.BuildDeps = if (@($pd.BuildDeps).Count -gt 0) { @($pd.BuildDeps) } else { @("setuptools>=68") }

        if ($override -and $override.ModelKind -eq "sizes") {
            $comp.ModelKind = "sizes"
            $comp.BuildRegisterArgs = $override.BuildRegisterArgs
            foreach ($sz in $override.SizeChoices) {
                $szDir = Join-Path $modelsDir $sz.Dirname
                if (Test-Path -LiteralPath $szDir) {
                    $comp.SizeChoices += [ordered]@{ Key = $sz.Key; Label = $sz.Label; Dir = $szDir; Bytes = (Get-DirSize $szDir) }
                }
            }
        }
        elseif ($override -and $override.ModelKind -eq "pair") {
            $comp.ModelKind = "pair"
            $comp.BuildRegisterArgs = $override.BuildRegisterArgs
            if ($hasModelsDir) {
                foreach ($sub in (Get-ChildItem -LiteralPath $modelsDir -Directory -ErrorAction SilentlyContinue)) {
                    $entry = [ordered]@{ Name = $sub.Name; Dir = $sub.FullName; Bytes = (Get-DirSize $sub.FullName) }
                    if ($sub.Name -match $override.OfflinePattern) { $comp.OfflineChoices += $entry } else { $comp.StreamingChoices += $entry }
                }
            }
        }
        elseif ($hasModelsDir) {
            # 通用兜底：没有专属表条目，但确实有 models/ 目录——列成复选，register 尽量猜（不保证对）。
            $comp.ModelKind = "generic"
            $comp.BuildRegisterArgs = {
                param($Selection)
                if (-not $Selection -or $Selection.Count -eq 0) { return $null }
                if ($Selection.Count -eq 1) { return @("register", "--model-dir", (Join-Path "{CompDir}" (Join-Path "models" $Selection[0]))) }
                return @("register", "--models-root", (Join-Path "{CompDir}" "models"))
            }
            foreach ($sub in (Get-ChildItem -LiteralPath $modelsDir -Directory -ErrorAction SilentlyContinue)) {
                $comp.SizeChoices += [ordered]@{ Key = $sub.Name; Label = $sub.Name; Dir = $sub.FullName; Bytes = (Get-DirSize $sub.FullName) }
            }
        }

        $comp.SourceBytes = Get-DirSize $d.FullName -ExcludeNames @(".venv*", "__pycache__", "models", "samples", ".pytest_cache", ".mypy_cache", ".ruff_cache") -ExcludeSuffixes @(".egg-info")
        $out += [pscustomobject]$comp
    }
    return $out
}

function Get-DirSize {
    # $ExcludeNames 里的项支持结尾通配符（".venv*" 匹配 ".venv"、".venv-rocm" 这类）——与上面 New-ToolboxBundle 里 robocopy /XD 用的是同一套写法，别让这俩表各自维护一份、迟早对不上。
    param([string]$Path, [string[]]$ExcludeNames = @(), [string[]]$ExcludeSuffixes = @())
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    try {
        $sum = 0L
        Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object {
            $p = $_.FullName
            $skip = $false
            foreach ($n in $ExcludeNames) {
                if ($n.EndsWith("*")) {
                    if ($p -match ("\\" + [regex]::Escape($n.TrimEnd("*")) + "[^\\]*\\")) { $skip = $true; break }
                } elseif ($p -match [regex]::Escape("\" + $n + "\")) { $skip = $true; break }
            }
            if (-not $skip) { foreach ($s in $ExcludeSuffixes) { if ($p -like "*$s\*" -or $p -like "*$s") { $skip = $true; break } } }
            if (-not $skip) { $sum += $_.Length }
        }
        return $sum
    } catch { return 0 }
}

function Format-Bytes {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return "{0:N1} GB" -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return "{0:N0} MB" -f ($Bytes / 1MB) }
    if ($Bytes -ge 1KB) { return "{0:N0} KB" -f ($Bytes / 1KB) }
    return "$Bytes B"
}

# ── 真正干活：拼出 staging 目录、写 manifest/setup.ps1/.cmd、压成 zip ──────────────────────
# Windows 对深/长路径的 Remove-Item -Recurse 不友好（torch 这类依赖里深嵌套的文件名轻松超过 MAX_PATH）——
# 用 robocopy /MIR 镜像一个空目录过去把内容清空（robocopy 自带长路径支持），再删空目录本身。
function Remove-DirLongPathSafe {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $empty = Join-Path ([System.IO.Path]::GetTempPath()) (".ruyi-empty-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $empty | Out-Null
    try {
        robocopy.exe $empty $Path /MIR /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
    } finally {
        Remove-Item -LiteralPath $empty -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# 模型目录动辄就上百个文件、完整路径很容易超过 Windows 的 260 字符上限（实测踩过：279 字符就让
# Copy-Item -Recurse 中途报“找不到路径”）——同样换成 robocopy（自带长路径支持）。
function Copy-DirLongPathSafe {
    param([string]$Source, [string]$Destination)
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    robocopy.exe $Source $Destination /E /NFL /NDL /NJH /NJS /NC /NS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw ("复制模型目录失败（robocopy 退出码 " + $LASTEXITCODE + "）：" + $Source) }
}

function New-ToolboxBundle {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string[]]$ComponentIds,
        [hashtable]$ModelSelection = @{},
        [Parameter(Mandatory)][string]$OutputDir,
        [Parameter(Mandatory)][string]$ZipName,
        [switch]$IncludeTests,
        [switch]$KeepStagingDir,
        # 组件 id -> 是否顺带打包依赖库（离线安装用）。asr-shim 这类有 GpuTorch 表的组件，值是选中的
        # GPU 变体 key（"cpu"／"nvidia"）；没有 GpuTorch 表的组件，任何非空真值就够（比如 "1"）。
        [hashtable]$OfflineDeps = @{},
        [scriptblock]$Log = { param($m) Write-Host $m }
    )

    $all = Get-ToolboxComponents -RepoRoot $RepoRoot
    $byId = @{}
    foreach ($c in $all) { $byId[$c.Id] = $c }

    $unknown = $ComponentIds | Where-Object { -not $byId.ContainsKey($_) }
    if ($unknown) {
        throw ("不认识这些组件 id：" + ($unknown -join ", ") + "。能打包的有：" + (($all | ForEach-Object { $_.Id }) -join ", "))
    }
    if ($ComponentIds.Count -eq 0) { throw "一个组件都没选，没什么可打包的。" }

    if (-not (Test-Path -LiteralPath $OutputDir)) { New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null }
    $OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path
    $zipPath = Join-Path $OutputDir ($ZipName + ".zip")
    # 组装过程在这里发生，跟用户要 zip 最终落在哪个目录是两回事——固定放到临时目录下一个短随机名，
    # 路径深度就与用户选哪儿当 -OutputDir 无关了（模型目录自己的文件名就很长，再加上用户选的输出目录，
    # 实测踩过：279 字符就让 Compress-Archive 中途报“找不到路径”——它跟上面 robocopy 那两处不一样，自己不支持长路径）。
    $stageDir = Join-Path ([System.IO.Path]::GetTempPath()) ("rtb-" + [guid]::NewGuid().ToString("N").Substring(0, 10))
    if (Test-Path -LiteralPath $stageDir) { Remove-DirLongPathSafe -Path $stageDir }
    New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

    $manifestComponents = @()
    $sizeTotal = 0L
    $uvBundled = $false   # uv.exe 只需要在 zip 里放一份，供所有带离线依赖的组件共用

    foreach ($id in $ComponentIds) {
        $c = $byId[$id]
        & $Log ("`n==> [" + $id + "] " + $c.Name)
        $destDir = Join-Path $stageDir $id
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null

        $xd = @(".venv*", "__pycache__", "models", "samples", ".pytest_cache", ".mypy_cache", ".ruff_cache", "*.egg-info")
        if (-not $IncludeTests) { $xd += "tests" }
        $roboArgs = @($c.Dir, $destDir, "/E", "/XD") + $xd + @("/XF", "*.pyc", "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS")
        & robocopy.exe @roboArgs | Out-Null
        if ($LASTEXITCODE -ge 8) { throw ("复制 " + $id + " 的源码失败（robocopy 退出码 " + $LASTEXITCODE + "）") }
        & $Log ("  源码已复制（不含 .venv/models/tests 等，见上面排除表）")

        $selection = $ModelSelection[$id]
        $bundledModelNames = @()
        if ($c.ModelKind -eq "sizes" -or $c.ModelKind -eq "generic") {
            $keys = @($selection)
            foreach ($choice in $c.SizeChoices) {
                if ($keys -contains $choice.Key) {
                    $dstModel = Join-Path $destDir (Join-Path "models" (Split-Path -Leaf $choice.Dir))
                    Copy-DirLongPathSafe -Source $choice.Dir -Destination $dstModel
                    $bundledModelNames += (Split-Path -Leaf $choice.Dir)
                    & $Log ("  已带上模型：" + $choice.Label + "（" + (Format-Bytes $choice.Bytes) + "）")
                }
            }
        }
        elseif ($c.ModelKind -eq "pair") {
            $sel = if ($selection) { $selection } else { @{} }
            foreach ($slot in @("Streaming", "Offline")) {
                $name = $sel[$slot]
                if (-not $name) { continue }
                $found = @($c.StreamingChoices; $c.OfflineChoices) | Where-Object { $_.Name -eq $name } | Select-Object -First 1
                if (-not $found) { throw ("组件 " + $id + " 没有叫 " + $name + " 的模型目录") }
                $dstModel = Join-Path $destDir (Join-Path "models" $found.Name)
                Copy-DirLongPathSafe -Source $found.Dir -Destination $dstModel
                $bundledModelNames += $found.Name
                & $Log ("  已带上模型（" + $slot + "）：" + $found.Name + "（" + (Format-Bytes $found.Bytes) + "）")
            }
        }

        $registerArgs = $null
        if ($bundledModelNames.Count -gt 0) {
            $sel = if ($c.ModelKind -eq "pair") { $selection } else { @($selection) }
            $registerArgs = & $c.BuildRegisterArgs $sel
        }
        if (-not $registerArgs) {
            if ($c.DownloadScriptRel) { & $Log ("  没带模型——目标机器会跑 " + $c.DownloadScriptRel + "（联网下载）") }
            else { & $Log ("  这个组件没有模型概念（或没找到下载脚本），目标机器只会跑 register（无参）") }
        }

        # 离线依赖：把这个组件的 wheel 也打进去，目标机器装环境这步就不用联网了（模型那部分本来就已经
        # 能离线——见上面；这里补的是 install.ps1 里 `uv pip install` 那几步）。setup.ps1 只认
        # 「这个组件目录下有没有 .offline-wheels」，不看 manifest——生成器与重放器各管各的，逻辑不重复。
        $offlineSel = $OfflineDeps[$id]
        $hasOfflineDeps = $false
        if ($offlineSel) {
            & $Log ("  打包依赖库（离线安装）……")
            $wheelsDir = Join-Path $destDir ".offline-wheels"
            $specs = @($c.BuildDeps) + @($c.PyDeps)
            Invoke-PipDownload -Specs $specs -DestDir $wheelsDir
            if ($c.GpuTorch -and ($offlineSel -is [string]) -and $c.GpuTorch.ContainsKey($offlineSel)) {
                $gpu = $c.GpuTorch[$offlineSel]
                & $Log ("    + PyTorch（" + $gpu.Label + "）……这一步可能要几分钟")
                Invoke-PipDownload -Specs @("torch") -DestDir $wheelsDir -IndexUrl $gpu.IndexUrl
            }
            $wheelCount = @(Get-ChildItem -LiteralPath $wheelsDir -Filter "*.whl" -ErrorAction SilentlyContinue).Count
            & $Log ("    已下 " + $wheelCount + " 个 wheel（" + (Format-Bytes (Get-DirSize $wheelsDir)) + "）——目标机器装环境这步不用联网了")
            $hasOfflineDeps = $true
            if (-not $uvBundled) {
                $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
                if ($uvCmd) {
                    $uvDst = Join-Path $stageDir "uv-portable"
                    New-Item -ItemType Directory -Force -Path $uvDst | Out-Null
                    Copy-Item -LiteralPath $uvCmd.Source -Destination (Join-Path $uvDst "uv.exe") -Force
                    & $Log ("  已带上 uv.exe——目标机器不用自己先装 uv")
                }
                $uvBundled = $true
            }
        }

        $destBytes = Get-DirSize $destDir
        $sizeTotal += $destBytes
        $manifestComponents += [ordered]@{
            id = $id; name = $c.Name; module = $c.Module
            models = $bundledModelNames
            offlineDeps = $hasOfflineDeps
            registerArgs = $registerArgs
            downloadScriptRel = $c.DownloadScriptRel
            bytes = $destBytes
        }
    }

    $manifest = [ordered]@{
        schema = 1
        createdAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
        components = $manifestComponents
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 8
    Set-Content -LiteralPath (Join-Path $stageDir "bundle-manifest.json") -Value $manifestJson -Encoding UTF8

    $readme = Get-BundleReadmeText -Manifest $manifest
    Set-Content -LiteralPath (Join-Path $stageDir "README.txt") -Value $readme -Encoding UTF8

    $setupPs1 = Get-SetupScriptText
    Set-Utf8BomFile -Path (Join-Path $stageDir "setup.ps1") -Text $setupPs1
    $setupCmd = "@echo off`r`nchcp 65001 >nul`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0setup.ps1`" %*`r`necho.`r`npause`r`n"
    Set-Utf8BomFile -Path (Join-Path $stageDir "安装并接入如意.cmd") -Text $setupCmd

    & $Log ("`n==> 压缩")
    # 压缩也在这个短临时目录里做（同样的长路径顾虑），压完只搬那一个 .zip 文件过去——单个文件搬家
    # 不会再撞见「某个模型文件的完整路径」这件事，跟目标目录多深没关系。
    $tmpZip = Join-Path ([System.IO.Path]::GetTempPath()) ("rtb-" + [guid]::NewGuid().ToString("N").Substring(0, 10) + ".zip")
    try {
        Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $tmpZip -CompressionLevel Optimal
    } catch {
        throw ("压缩失败——多半还是路径太长（Windows 经典上限 260 字符）。到系统设置搜「启用 Win32 长路径」打开，" +
            "或者装了更少的模型再试。原始错误：" + $_.Exception.Message)
    }
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    Move-Item -LiteralPath $tmpZip -Destination $zipPath -Force
    $zipBytes = (Get-Item -LiteralPath $zipPath).Length
    & $Log ("  " + $zipPath + "（" + (Format-Bytes $zipBytes) + "）")

    if (-not $KeepStagingDir) {
        Remove-DirLongPathSafe -Path $stageDir
    } else {
        $keepDir = Join-Path $OutputDir $ZipName
        if (Test-Path -LiteralPath $keepDir) { Remove-DirLongPathSafe -Path $keepDir }
        New-Item -ItemType Directory -Force -Path $keepDir | Out-Null
        robocopy.exe $stageDir $keepDir /E /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        Remove-DirLongPathSafe -Path $stageDir
        & $Log ("  解压后的目录也留着了：" + $keepDir)
    }

    return [pscustomobject]@{
        Ok = $true
        ZipPath = $zipPath
        ZipBytes = $zipBytes
        Components = $manifestComponents
    }
}

function Set-Utf8BomFile {
    param([string]$Path, [string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $bom = [byte[]](0xEF, 0xBB, 0xBF)
    [System.IO.File]::WriteAllBytes($Path, $bom + $bytes)
}

function Get-BundleReadmeText {
    param($Manifest)
    $lines = @()
    $lines += "如意工作台 · 扩展组件安装包"
    $lines += "================================"
    $lines += ""
    $lines += "打包时间：" + $Manifest.createdAt
    $lines += "包含组件：" + (($Manifest.components | ForEach-Object { $_.name + "（" + $_.id + "）" }) -join "、")
    $lines += ""
    $lines += "怎么装："
    $lines += "  1. 解压这个压缩包到任意目录（不要放进如意工作台自己的安装目录里）。"
    $lines += "  2. 双击「安装并接入如意.cmd」（第一次可能被 Windows SmartScreen 拦一下，点「更多信息 → 仍要运行」）。"
    $lines += "  3. 等它跑完——每个组件会建自己的 Python 虚拟环境、装依赖（这一步要联网），"
    $lines += "     然后向如意登记自己。"
    $lines += "  4. 重启如意工作台（或第一次启动），设置页「MCP／扩展组件」栏应该能看到新组件了。"
    $lines += ""
    $lines += "先决条件：机器上要能用 uv（https://astral.sh/uv）。没有的话 setup.ps1 会告诉你怎么装，"
    $lines += "或者直接用 -InstallUv 参数让它自动装：powershell -ExecutionPolicy Bypass -File .\setup.ps1 -InstallUv"
    $lines += ""
    foreach ($c in $Manifest.components) {
        $lines += ("· " + $c.name + "（" + $c.id + "）")
        if ($c.models -and $c.models.Count -gt 0) {
            $lines += ("    已带上模型：" + ($c.models -join "、") + " —— 不用再联网下载这部分。")
        } elseif ($c.downloadScriptRel) {
            $lines += "    没带模型——装好环境后会自动联网下载（跟全新安装一样）。"
        }
    }
    $lines += ""
    $lines += "本包由 ruyi-toolbox/tools/package-bundle.ps1 生成。源仓库：https://github.com/wangzhe04/ruyi-toolbox"
    return ($lines -join "`r`n")
}

# ── 生成的 setup.ps1：在目标机器上跑，纯粹按 bundle-manifest.json 的记录重放，不认识别的逻辑 ──
function Get-SetupScriptText {
    return @'
#Requires -Version 5.1
<#
.SYNOPSIS
  把这个压缩包里带着的组件都装起来、向如意工作台登记。双击旁边那个「安装并接入如意.cmd」等于跑这个脚本。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup.ps1
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Only asr-shim
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -InstallUv
#>
[CmdletBinding()]
param(
    [string[]]$Only = @(),
    [string[]]$Skip = @(),
    [switch]$InstallUv
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$manifestPath = Join-Path $root "bundle-manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    Write-Host "找不到 bundle-manifest.json——这个脚本得跟打包出来的那一整套文件放在一起，别单独挪走。" -ForegroundColor Red
    exit 1
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

# 带了离线依赖的组件会顺带打包 uv.exe（见 uv-portable\），有就优先用它——目标机器不用先自己装 uv。
$bundledUvExe = Join-Path $root "uv-portable\uv.exe"
if (Test-Path -LiteralPath $bundledUvExe) {
    $env:Path = (Split-Path -Parent $bundledUvExe) + ";" + $env:Path
}
# 同样的逗号拆分问题（见 package-bundle.ps1 那一处注释），-Only/-Skip 也要拆。
if ($Only) { $Only = @($Only | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
if ($Skip) { $Skip = @($Skip | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }

function Test-Uv {
    return [bool](Get-Command uv -ErrorAction SilentlyContinue)
}

Write-Host "如意工作台 · 扩展组件安装包" -ForegroundColor Cyan
Write-Host ("包含：" + (($manifest.components | ForEach-Object { $_.name }) -join "、"))
Write-Host ""

if (-not (Test-Uv)) {
    if ($InstallUv) {
        Write-Host "==> 没找到 uv，按 -InstallUv 自动装它" -ForegroundColor Cyan
        powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
        $cand = Join-Path $env:USERPROFILE ".local\bin"
        if (Test-Path -LiteralPath $cand) { $env:Path = $cand + ";" + $env:Path }
        if (-not (Test-Uv)) {
            Write-Host "装完了但这个会话里还是找不到 uv——关掉这个窗口，重新开一个再跑一次本脚本。" -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host "没找到 uv（各组件建虚拟环境要用它）。装它：" -ForegroundColor Red
        Write-Host '  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
        Write-Host "装完重开一个 PowerShell 窗口，或者直接给本脚本加 -InstallUv 参数让它自动装。"
        exit 1
    }
}

$okCount = 0
$failCount = 0
foreach ($c in $manifest.components) {
    if ($Only.Count -gt 0 -and ($Only -notcontains $c.id)) { continue }
    if ($Skip -contains $c.id) { continue }

    Write-Host ("`n==> [" + $c.id + "] " + $c.name) -ForegroundColor Cyan
    $compDir = Join-Path $root $c.id
    if (-not (Test-Path -LiteralPath $compDir)) {
        Write-Host ("  这个组件的目录不在——" + $compDir) -ForegroundColor Red
        $failCount++; continue
    }
    $installScript = Join-Path $compDir "scripts\install.ps1"
    # 带了离线依赖（.offline-wheels\）就让 uv 完全不碰网络——只认本地这份 wheel；install.ps1 本身
    # 一个字不改，它照样调用裸 `uv`，这两个环境变量是 uv 自己认的开关（uv pip install --help 里
    # 写着 [env: UV_OFFLINE=] / [env: UV_FIND_LINKS=]）。跑完这一个组件就把两个变量收掉，不连累下一个。
    $offlineWheels = Join-Path $compDir ".offline-wheels"
    $usingOffline = Test-Path -LiteralPath $offlineWheels
    if ($usingOffline) {
        Write-Host "  带着离线依赖——这步不联网"
        $env:UV_OFFLINE = "1"
        $env:UV_FIND_LINKS = $offlineWheels
    }
    try {
        & $installScript
        if ($LASTEXITCODE -ne 0) { throw ("install.ps1 退出码 " + $LASTEXITCODE) }
    } catch {
        Write-Host ("  装环境这步失败：" + $_.Exception.Message) -ForegroundColor Red
        $failCount++; continue
    } finally {
        if ($usingOffline) { Remove-Item Env:\UV_OFFLINE, Env:\UV_FIND_LINKS -ErrorAction SilentlyContinue }
    }

    $py = Join-Path $compDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $py)) {
        Write-Host "  install.ps1 跑完了但没看到 .venv\Scripts\python.exe，跳过登记这步。" -ForegroundColor Red
        $failCount++; continue
    }

    $registered = $false
    if ($c.registerArgs -and $c.registerArgs.Count -gt 0) {
        $regArgs = $c.registerArgs | ForEach-Object { $_ -replace [regex]::Escape("{CompDir}"), $compDir }
        Write-Host "  已经带着模型——直接登记（不用再联网下模型）"
        try {
            & $py -m $c.module @regArgs
            if ($LASTEXITCODE -ne 0) { throw ("register 退出码 " + $LASTEXITCODE) }
            $registered = $true
        } catch {
            Write-Host ("  登记失败：" + $_.Exception.Message) -ForegroundColor Red
        }
    } elseif ($c.downloadScriptRel) {
        Write-Host "  没带模型——跑这个组件自己的下载脚本（联网）"
        try {
            & (Join-Path $compDir $c.downloadScriptRel)
            if ($LASTEXITCODE -ne 0) { throw ("下载脚本退出码 " + $LASTEXITCODE) }
            $registered = $true
        } catch {
            Write-Host ("  下模型/登记失败：" + $_.Exception.Message) -ForegroundColor Red
        }
    } else {
        Write-Host "  这个组件没有模型概念，直接登记"
        try {
            & $py -m $c.module register
            if ($LASTEXITCODE -ne 0) { throw ("register 退出码 " + $LASTEXITCODE) }
            $registered = $true
        } catch {
            Write-Host ("  登记失败：" + $_.Exception.Message) -ForegroundColor Red
        }
    }

    if ($registered) { $okCount++ } else { $failCount++ }
}

Write-Host ""
if ($failCount -eq 0) {
    Write-Host ("全部 " + $okCount + " 个组件都装好并登记了。重启如意工作台（或第一次启动），设置页「MCP／扩展组件」栏应该能看到。") -ForegroundColor Green
} else {
    Write-Host ($okCount.ToString() + " 个成功，" + $failCount.ToString() + " 个没成功（见上面各自的原因）。") -ForegroundColor Yellow
}
exit $failCount
'@
}

# ── 可视化窗口 ────────────────────────────────────────────────────────────────────────────
function Show-PackagerGui {
    param([string]$RepoRoot)

    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $components = Get-ToolboxComponents -RepoRoot $RepoRoot

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "如意扩展组件打包器 —— ruyi-toolbox"
    $form.Size = New-Object System.Drawing.Size(780, 700)
    $form.MinimumSize = New-Object System.Drawing.Size(600, 500)
    $form.StartPosition = "CenterScreen"

    $hint = New-Object System.Windows.Forms.Label
    $hint.Text = "勾选要打包的组件（与顺带打包的已下模型，可选），生成一个压缩包。`r`n在新电脑上解压后双击里面的「安装并接入如意.cmd」即可，不用再敲命令。"
    $hint.AutoSize = $false
    $hint.Location = New-Object System.Drawing.Point(12, 10)
    $hint.Size = New-Object System.Drawing.Size(740, 40)
    $hint.Anchor = "Top,Left,Right"
    $form.Controls.Add($hint)

    $listPanel = New-Object System.Windows.Forms.Panel
    $listPanel.AutoScroll = $true
    $listPanel.Location = New-Object System.Drawing.Point(12, 55)
    $listPanel.Size = New-Object System.Drawing.Size(742, 330)
    $listPanel.Anchor = "Top,Left,Right,Bottom"
    $listPanel.BorderStyle = "FixedSingle"
    $form.Controls.Add($listPanel)

    if ($components.Count -eq 0) {
        $none = New-Object System.Windows.Forms.Label
        $none.Text = "没找到能打包的组件（要有 pyproject.toml 和 scripts\install.ps1）。"
        $none.AutoSize = $true
        $none.Location = New-Object System.Drawing.Point(10, 10)
        $listPanel.Controls.Add($none)
    }

    $rows = @{}
    $y = 8
    foreach ($c in $components) {
        $gbHeight = 46 + 22
        if ($c.ModelKind -eq "sizes" -or $c.ModelKind -eq "generic") { $gbHeight += 22 * [Math]::Max(1, $c.SizeChoices.Count) }
        if ($c.ModelKind -eq "pair") { $gbHeight += 50 }
        if ($c.GpuTorch) { $gbHeight += 24 }

        $gb = New-Object System.Windows.Forms.GroupBox
        $gb.Text = $c.Name + "  [" + $c.Id + "]  ·  源码约 " + (Format-Bytes $c.SourceBytes)
        $gb.Location = New-Object System.Drawing.Point(8, $y)
        $gb.Size = New-Object System.Drawing.Size(700, $gbHeight)
        $gb.Anchor = "Top,Left,Right"

        $chkInclude = New-Object System.Windows.Forms.CheckBox
        $chkInclude.Text = "打包这个组件"
        $chkInclude.Checked = $true
        $chkInclude.Location = New-Object System.Drawing.Point(12, 20)
        $chkInclude.AutoSize = $true
        $gb.Controls.Add($chkInclude)

        $row = @{ Include = $chkInclude; Kind = $c.ModelKind; SizeChecks = @{}; StreamCombo = $null; OfflineCombo = $null; OfflineDepsChk = $null; GpuCombo = $null }

        $my = 44
        if ($c.ModelKind -eq "sizes" -or $c.ModelKind -eq "generic") {
            if ($c.SizeChoices.Count -eq 0) {
                $lbl = New-Object System.Windows.Forms.Label
                $lbl.Text = "（这台机器上还没下过模型——目标机器会自动联网下载）"
                $lbl.ForeColor = [System.Drawing.Color]::Gray
                $lbl.Location = New-Object System.Drawing.Point(30, $my)
                $lbl.AutoSize = $true
                $gb.Controls.Add($lbl)
                $my += 22
            } else {
                foreach ($choice in $c.SizeChoices) {
                    $chk = New-Object System.Windows.Forms.CheckBox
                    $chk.Text = "顺带打包已下的模型：" + $choice.Label + "（" + (Format-Bytes $choice.Bytes) + "）"
                    $chk.Checked = $true
                    $chk.Location = New-Object System.Drawing.Point(30, $my)
                    $chk.AutoSize = $true
                    $gb.Controls.Add($chk)
                    $row.SizeChecks[$choice.Key] = $chk
                    $my += 22
                }
            }
        }
        elseif ($c.ModelKind -eq "pair") {
            $lblS = New-Object System.Windows.Forms.Label
            $lblS.Text = "流式（边说边出字）用哪份模型："
            $lblS.Location = New-Object System.Drawing.Point(30, $my)
            $lblS.AutoSize = $true
            $gb.Controls.Add($lblS)
            $cbS = New-Object System.Windows.Forms.ComboBox
            $cbS.DropDownStyle = "DropDownList"
            $cbS.Location = New-Object System.Drawing.Point(230, ($my - 3))
            $cbS.Size = New-Object System.Drawing.Size(420, 22)
            [void]$cbS.Items.Add("（不带——目标机器联网下载）")
            foreach ($m in $c.StreamingChoices) { [void]$cbS.Items.Add($m.Name + "（" + (Format-Bytes $m.Bytes) + "）") }
            $cbS.SelectedIndex = if ($c.StreamingChoices.Count -gt 0) { 1 } else { 0 }
            $gb.Controls.Add($cbS)
            $row.StreamCombo = $cbS
            $my += 24

            $lblO = New-Object System.Windows.Forms.Label
            $lblO.Text = "离线整句（句尾重听）用哪份模型："
            $lblO.Location = New-Object System.Drawing.Point(30, $my)
            $lblO.AutoSize = $true
            $gb.Controls.Add($lblO)
            $cbO = New-Object System.Windows.Forms.ComboBox
            $cbO.DropDownStyle = "DropDownList"
            $cbO.Location = New-Object System.Drawing.Point(230, ($my - 3))
            $cbO.Size = New-Object System.Drawing.Size(420, 22)
            [void]$cbO.Items.Add("（不带——目标机器联网下载）")
            foreach ($m in $c.OfflineChoices) { [void]$cbO.Items.Add($m.Name + "（" + (Format-Bytes $m.Bytes) + "）") }
            $cbO.SelectedIndex = if ($c.OfflineChoices.Count -gt 0) { 1 } else { 0 }
            $gb.Controls.Add($cbO)
            $row.OfflineCombo = $cbO
            $my += 24
        }
        else {
            $lbl = New-Object System.Windows.Forms.Label
            $lbl.Text = "（这个组件没有模型概念）"
            $lbl.ForeColor = [System.Drawing.Color]::Gray
            $lbl.Location = New-Object System.Drawing.Point(30, $my)
            $lbl.AutoSize = $true
            $gb.Controls.Add($lbl)
            $my += 22
        }

        # 133e 风格的独立开关：不管上面模型那部分是哪个分支，$my 到这里都已经落在「下一空行」——
        # 离线依赖打包与模型选择相互独立（哪怕这次没带模型，也可能只想先把依赖库备好）。
        $chkOffline = New-Object System.Windows.Forms.CheckBox
        $chkOffline.Text = "打包依赖库（完全离线安装，装环境这步也不用联网；体积会明显变大）"
        $chkOffline.Checked = $false
        $chkOffline.Location = New-Object System.Drawing.Point(12, $my)
        $chkOffline.AutoSize = $true
        $gb.Controls.Add($chkOffline)
        $row.OfflineDepsChk = $chkOffline
        $my += 22
        if ($c.GpuTorch) {
            $lblGpu = New-Object System.Windows.Forms.Label
            $lblGpu.Text = "打包哪种显卡的 PyTorch："
            $lblGpu.Location = New-Object System.Drawing.Point(30, $my)
            $lblGpu.AutoSize = $true
            $gb.Controls.Add($lblGpu)
            $cbGpu = New-Object System.Windows.Forms.ComboBox
            $cbGpu.DropDownStyle = "DropDownList"
            $cbGpu.Location = New-Object System.Drawing.Point(230, ($my - 3))
            $cbGpu.Size = New-Object System.Drawing.Size(420, 22)
            $gpuKeys = @($c.GpuTorch.Keys)
            foreach ($k in $gpuKeys) { [void]$cbGpu.Items.Add($c.GpuTorch[$k].Label) }
            $cbGpu.Tag = $gpuKeys
            $cbGpu.SelectedIndex = 0
            $gb.Controls.Add($cbGpu)
            $row.GpuCombo = $cbGpu
        }

        $listPanel.Controls.Add($gb)
        $rows[$c.Id] = $row
        $y += $gbHeight + 8
    }

    $y2 = 395
    $outLbl = New-Object System.Windows.Forms.Label
    $outLbl.Text = "输出目录："
    $outLbl.Location = New-Object System.Drawing.Point(12, $y2)
    $outLbl.AutoSize = $true
    $form.Controls.Add($outLbl)
    $outBox = New-Object System.Windows.Forms.TextBox
    $outBox.Text = Join-Path $RepoRoot "dist"
    $outBox.Location = New-Object System.Drawing.Point(90, ($y2 - 3))
    $outBox.Size = New-Object System.Drawing.Size(540, 22)
    $outBox.Anchor = "Top,Left,Right"
    $form.Controls.Add($outBox)
    $browseBtn = New-Object System.Windows.Forms.Button
    $browseBtn.Text = "浏览…"
    $browseBtn.Location = New-Object System.Drawing.Point(640, ($y2 - 4))
    $browseBtn.Size = New-Object System.Drawing.Size(70, 24)
    $browseBtn.Anchor = "Top,Right"
    $browseBtn.Add_Click({
        $fbd = New-Object System.Windows.Forms.FolderBrowserDialog
        $fbd.SelectedPath = $outBox.Text
        if ($fbd.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $outBox.Text = $fbd.SelectedPath }
    })
    $form.Controls.Add($browseBtn)

    $y2 += 28
    $nameLbl = New-Object System.Windows.Forms.Label
    $nameLbl.Text = "压缩包名："
    $nameLbl.Location = New-Object System.Drawing.Point(12, $y2)
    $nameLbl.AutoSize = $true
    $form.Controls.Add($nameLbl)
    $nameBox = New-Object System.Windows.Forms.TextBox
    $nameBox.Text = "ruyi-toolbox-bundle-" + (Get-Date -Format "yyyyMMdd")
    $nameBox.Location = New-Object System.Drawing.Point(90, ($y2 - 3))
    $nameBox.Size = New-Object System.Drawing.Size(300, 22)
    $form.Controls.Add($nameBox)

    $y2 += 28
    $chkTests = New-Object System.Windows.Forms.CheckBox
    $chkTests.Text = "包含测试代码（tests/，一般不需要）"
    $chkTests.Location = New-Object System.Drawing.Point(12, $y2)
    $chkTests.AutoSize = $true
    $form.Controls.Add($chkTests)
    $chkKeep = New-Object System.Windows.Forms.CheckBox
    $chkKeep.Text = "打包后保留解压好的目录（便于检查）"
    $chkKeep.Location = New-Object System.Drawing.Point(300, $y2)
    $chkKeep.AutoSize = $true
    $form.Controls.Add($chkKeep)

    $y2 += 30
    $btnPack = New-Object System.Windows.Forms.Button
    $btnPack.Text = "开始打包"
    $btnPack.Location = New-Object System.Drawing.Point(12, $y2)
    $btnPack.Size = New-Object System.Drawing.Size(110, 32)
    $form.Controls.Add($btnPack)
    $btnOpen = New-Object System.Windows.Forms.Button
    $btnOpen.Text = "打开输出目录"
    $btnOpen.Location = New-Object System.Drawing.Point(132, $y2)
    $btnOpen.Size = New-Object System.Drawing.Size(110, 32)
    $btnOpen.Enabled = $false
    $form.Controls.Add($btnOpen)

    $y2 += 40
    $logBox = New-Object System.Windows.Forms.TextBox
    $logBox.Multiline = $true
    $logBox.ReadOnly = $true
    $logBox.ScrollBars = "Vertical"
    $logBox.Font = New-Object System.Drawing.Font("Consolas", 9)
    $logBox.Location = New-Object System.Drawing.Point(12, $y2)
    $logBox.Size = New-Object System.Drawing.Size(742, 150)
    $logBox.Anchor = "Left,Right,Bottom"
    $form.Controls.Add($logBox)

    $lastZipPath = $null
    $btnOpen.Add_Click({ if ($lastZipPath) { Start-Process explorer.exe -ArgumentList ("/select,`"" + $lastZipPath + "`"") } })

    $btnPack.Add_Click({
        $selectedIds = @()
        $modelSel = @{}
        $offlineDeps = @{}
        foreach ($c in $components) {
            $row = $rows[$c.Id]
            if (-not $row.Include.Checked) { continue }
            $selectedIds += $c.Id
            if ($row.Kind -eq "sizes" -or $row.Kind -eq "generic") {
                $keys = @()
                foreach ($k in $row.SizeChecks.Keys) { if ($row.SizeChecks[$k].Checked) { $keys += $k } }
                $modelSel[$c.Id] = $keys
            } elseif ($row.Kind -eq "pair") {
                $streamName = if ($row.StreamCombo.SelectedIndex -gt 0) { $c.StreamingChoices[$row.StreamCombo.SelectedIndex - 1].Name } else { "" }
                $offlineName = if ($row.OfflineCombo.SelectedIndex -gt 0) { $c.OfflineChoices[$row.OfflineCombo.SelectedIndex - 1].Name } else { "" }
                $modelSel[$c.Id] = @{ Streaming = $streamName; Offline = $offlineName }
            }
            if ($row.OfflineDepsChk -and $row.OfflineDepsChk.Checked) {
                if ($row.GpuCombo) {
                    $gpuKeys = $row.GpuCombo.Tag
                    $offlineDeps[$c.Id] = $gpuKeys[$row.GpuCombo.SelectedIndex]
                } else {
                    $offlineDeps[$c.Id] = "1"
                }
            }
        }
        if ($selectedIds.Count -eq 0) {
            [System.Windows.Forms.MessageBox]::Show("一个组件都没勾，没什么可打包的。", "提示") | Out-Null
            return
        }
        if (-not $outBox.Text.Trim() -or -not $nameBox.Text.Trim()) {
            [System.Windows.Forms.MessageBox]::Show("输出目录和压缩包名都不能空着。", "提示") | Out-Null
            return
        }

        $form.Controls | ForEach-Object { $_.Enabled = $false }
        $logBox.Enabled = $true
        $logBox.Clear()
        $btnOpen.Enabled = $false
        try {
            $logCb = { param($m) $logBox.AppendText($m + "`r`n"); $logBox.SelectionStart = $logBox.TextLength; $logBox.ScrollToCaret(); [System.Windows.Forms.Application]::DoEvents() }
            $result = New-ToolboxBundle -RepoRoot $RepoRoot -ComponentIds $selectedIds -ModelSelection $modelSel -OfflineDeps $offlineDeps `
                -OutputDir $outBox.Text.Trim() -ZipName $nameBox.Text.Trim() `
                -IncludeTests:$chkTests.Checked -KeepStagingDir:$chkKeep.Checked -Log $logCb
            & $logCb ("`n完成！" + (Format-Bytes $result.ZipBytes) + " —— " + $result.ZipPath)
            $lastZipPath = $result.ZipPath
            $btnOpen.Enabled = $true
        } catch {
            $logBox.AppendText("`r`n打包失败：" + $_.Exception.Message + "`r`n")
        } finally {
            $form.Controls | ForEach-Object { $_.Enabled = $true }
        }
    })

    [void]$form.ShowDialog()
}

# ── 分发：给了 -Components 就走命令行、不开窗口；否则弹窗口 ──────────────────────────────────
function ConvertFrom-ModelsSpec {
    param([string]$Spec)
    # "asr-shim=0.6b,1.7b;asr-stream=streaming:NAME,offline:NAME"
    $out = @{}
    if (-not $Spec.Trim()) { return $out }
    foreach ($seg in ($Spec -split ';')) {
        if (-not $seg.Trim()) { continue }
        $parts = $seg -split '=', 2
        if ($parts.Count -ne 2) { continue }
        $id = $parts[0].Trim()
        $vals = $parts[1].Trim()
        if ($vals -match ':') {
            $pair = @{}
            foreach ($kv in ($vals -split ',')) {
                $kvp = $kv -split ':', 2
                if ($kvp.Count -eq 2) {
                    if ($kvp[0].Trim() -eq 'streaming') { $pair.Streaming = $kvp[1].Trim() }
                    elseif ($kvp[0].Trim() -eq 'offline') { $pair.Offline = $kvp[1].Trim() }
                }
            }
            $out[$id] = $pair
        } else {
            $out[$id] = @($vals -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        }
    }
    return $out
}

function ConvertFrom-OfflineDepsSpec {
    param([string]$Spec)
    # "asr-shim=nvidia;asr-stream=1" —— 见上面 -OfflineDeps 参数的说明。
    $out = @{}
    if (-not $Spec.Trim()) { return $out }
    foreach ($seg in ($Spec -split ';')) {
        if (-not $seg.Trim()) { continue }
        $parts = $seg -split '=', 2
        if ($parts.Count -eq 2 -and $parts[1].Trim()) { $out[$parts[0].Trim()] = $parts[1].Trim() }
    }
    return $out
}

# 点号源（. .\package-bundle.ps1）时只把上面那些函数带进当前作用域，不自动开窗口也不自动打包
# （给其它脚本/测试复用 Get-ToolboxComponents 、 New-ToolboxBundle 用）。
if ($MyInvocation.InvocationName -ne '.') {
    if ($PSBoundParameters.ContainsKey('Components')) {
        $od = if ($OutputDir) { $OutputDir } else { Join-Path $Root "dist" }
        $zn = if ($ZipName) { $ZipName } else { "ruyi-toolbox-bundle-" + (Get-Date -Format "yyyyMMdd") }
        $modelSel = ConvertFrom-ModelsSpec -Spec $Models
        $offlineDepsSel = ConvertFrom-OfflineDepsSpec -Spec $OfflineDeps
        $result = New-ToolboxBundle -RepoRoot $Root -ComponentIds $Components -ModelSelection $modelSel -OfflineDeps $offlineDepsSel `
            -OutputDir $od -ZipName $zn -IncludeTests:$IncludeTests -KeepStagingDir:$KeepStagingDir
        Write-Host ("`n完成：" + $result.ZipPath)
        exit 0
    } else {
        Show-PackagerGui -RepoRoot $Root
    }
}
