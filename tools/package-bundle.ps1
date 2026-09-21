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
  装依赖（打包时勾了「打包依赖库」的组件这一步不联网，用包里带的轮子；没勾的跟手动跑 install.ps1 一样要联网），然后：
    · 这次打包带了模型 —— 直接向如意登记（不再触网下模型）；
    · 没带模型 —— 跑该组件自己的 download-model.ps1（联网下模型再登记，跟全新安装一样）。

  「打包依赖库」是本机现成的优先、缺的才下载（见 tools\README.md「离线依赖库」）；打包过程有进度条与剩余时间，
  可以随时取消，动手之前会先预检磁盘空间与网络（见「进度、取消与预检」）。

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
    # 有 GpuTorch 表的组件（目前只有 asr-shim）给 GPU 变体 key："nvidia"、"amd" 或 "cpu"
    # （本机 venv 里装的是哪种，那种就直接用、不用下载；选别的才会去下）。
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
# tools\ 目录本身（localwheels.py 住在这里）——测试用 -Root 指向假仓库时，它仍指向真的 tools\。
$script:ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path

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
        # torch 不在 pyproject.toml 的 dependencies 里（install.ps1 自己按显卡挑索引装）。打包离线依赖时：
        #   · 本机 venv 里装的那一种构建（看 torch 版本号里的 +cu128／+cpu／+rocm）直接还原成轮子，不用下；
        #   · 选了本机没有的构建才去下——nvidia／cpu 走 PyTorch 的 index，amd 走 AMD 官方直链（见下面 Rocm）。
        # GpuOrder ＝ 界面下拉框里的先后顺序（Hashtable 自己不保序）。
        GpuOrder = @("nvidia", "amd", "cpu")
        GpuTorch = @{
            nvidia = @{ Label = "NVIDIA（CUDA cu128）"; IndexUrl = "https://download.pytorch.org/whl/cu128"; Approx = "约 3 GB"; Bytes = 2750MB }
            amd    = @{ Label = "AMD（ROCm on Windows，⚠ 未经真机验证）"; Rocm = $true; Approx = "约 2.2 GB"; Bytes = 2200MB }
            cpu    = @{ Label = "CPU（体积小，推理慢）"; IndexUrl = "https://download.pytorch.org/whl/cpu"; Approx = "约 200 MB"; Bytes = 200MB }
        }
        # AMD 那几个直链的版本号；必须跟 asr-shim\scripts\install.ps1 里的 $RocmRelease／torch-x.y.z 保持一致
        # （打包时会核对，对不上就报错，不会悄悄打出一个错版本的包）。
        Rocm = @{ Release = "7.2.1"; TorchVersion = "2.9.1" }
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

# ── 跑外部命令：输出实时回显、有总时限 ────────────────────────────────────────────────────────
# 以前是直接 `& pip download ... --quiet`：没有一行输出、没有时限，代理一挂住就是无限期"卡在这一步"，
# 而且图形界面整个不响应。这里改成：输出实时打进日志、每 30 秒没动静就报一次"还在跑"、超时就杀掉并说清楚。
$script:GuiActive = $false   # Show-PackagerGui 置 true；命令行模式等待时不需要刷新窗口

function Update-GuiIfActive {
    if ($script:GuiActive) { [System.Windows.Forms.Application]::DoEvents() }
}

# ── 进度 ────────────────────────────────────────────────────────────────────────────────────
# 各阶段按"估算耗时（秒）"配权重：总进度 ＝ 已完成阶段的权重 ＋ 当前阶段的完成比例 × 它的权重。
# 不用字节当权重，是因为各阶段速度差得太远（复制模型几百 MB/s、还原轮子要压缩只有几十 MB/s、走代理下载才几 MB/s），
# 按字节算，进度条会在下载那段停很久、其它段一闪而过。阶段【内部】的比例全是实测的：复制看目标目录长了多少、
# 还原轮子由 localwheels.py 汇报字节、下载读 pip／curl 的进度行、压缩按写入的字节数。
# 剩余时间＝已用时间 ÷ 已完成比例 × 剩余比例（拿实测速度校准，比一开始的估算准）。
$script:Prog = $null
$script:CancelRequested = $false     # 界面上的「取消」按钮（或关闭窗口）置 true；长循环里会检查它
$script:LastLogPct = -100            # 命令行模式下，进度每前进 5% 才打一行 [NN%]（重定向到文件时看不到 Write-Progress）

function Format-Duration {
    param([double]$Seconds)
    if ($Seconds -lt 0 -or [double]::IsNaN($Seconds)) { return "--:--" }
    $t = [TimeSpan]::FromSeconds([Math]::Round($Seconds))
    if ($t.TotalHours -ge 1) { return ("{0}:{1:00}:{2:00}" -f [int][Math]::Floor($t.TotalHours), $t.Minutes, $t.Seconds) }
    return ("{0}:{1:00}" -f [int][Math]::Floor($t.TotalMinutes), $t.Seconds)
}

function Initialize-Progress {
    param([double]$TotalWeight, [scriptblock]$Report)
    $script:LastLogPct = -100
    $script:Prog = @{
        Total = [Math]::Max($TotalWeight, 1.0); Done = 0.0; StageW = 0.0; Frac = 0.0
        Label = ""; Detail = ""; Report = $Report
        Sw = [System.Diagnostics.Stopwatch]::StartNew(); LastMs = -1000
    }
}

function Get-ProgressSnapshot {
    $p = $script:Prog
    $frac = ($p.Done + $p.StageW * $p.Frac) / $p.Total
    if ($frac -gt 1.0) { $frac = 1.0 }
    if ($frac -lt 0.0) { $frac = 0.0 }
    $elapsed = $p.Sw.Elapsed.TotalSeconds
    $eta = -1.0
    if ($frac -ge 0.03 -and $frac -lt 1.0) { $eta = $elapsed / $frac * (1.0 - $frac) }
    return [pscustomobject]@{
        Fraction = $frac; Percent = [int][Math]::Floor($frac * 100.0 + 1e-6)
        Label = [string]$p.Label; Detail = [string]$p.Detail; ElapsedSec = $elapsed; EtaSec = $eta
    }
}

function Send-ProgressReport {
    param([switch]$Force)
    $p = $script:Prog
    if (-not $p -or -not $p.Report) { return }
    $ms = $p.Sw.ElapsedMilliseconds
    if (-not $Force -and ($ms - $p.LastMs) -lt 120) { return }   # 别把界面刷卡了：至多每 0.12 秒一次
    $p.LastMs = $ms
    $null = & $p.Report (Get-ProgressSnapshot)
}

function Enter-ProgressStage {
    param([string]$Label, [double]$Weight)
    if (-not $script:Prog) { return }
    $script:Prog.Label = $Label
    $script:Prog.Detail = ""
    $script:Prog.StageW = [Math]::Max($Weight, 0.0)
    $script:Prog.Frac = 0.0
    Send-ProgressReport -Force
}

function Set-ProgressFraction {
    param([double]$Fraction, [string]$Detail = "")
    if (-not $script:Prog) { return }
    if ($Fraction -lt 0.0) { $Fraction = 0.0 } elseif ($Fraction -gt 1.0) { $Fraction = 1.0 }
    $script:Prog.Frac = $Fraction
    if ($Detail) { $script:Prog.Detail = $Detail }
    Send-ProgressReport
}

function Exit-ProgressStage {
    if (-not $script:Prog) { return }
    $script:Prog.Done += $script:Prog.StageW
    $script:Prog.StageW = 0.0
    $script:Prog.Frac = 0.0
    Send-ProgressReport -Force
}

function Test-CancelRequested {
    if ($script:CancelRequested) { throw "已取消。" }
}

# 命令行模式的进度显示：Write-Progress 画进度条；同时每前进 5% 打一行 [NN%]，日志重定向到文件时也看得到。
function New-ConsoleReporter {
    return {
        param($s)
        $eta = if ($s.EtaSec -ge 0) { "，约剩 " + (Format-Duration $s.EtaSec) } else { "" }
        Write-Progress -Activity "打包 ruyi-toolbox" -PercentComplete $s.Percent `
            -Status ("{0}%  {1}  {2}   已用 {3}{4}" -f $s.Percent, $s.Label, $s.Detail, (Format-Duration $s.ElapsedSec), $eta)
        if ($s.Percent -ge ($script:LastLogPct + 5) -or ($s.Percent -ge 100 -and $script:LastLogPct -lt 100)) {
            $script:LastLogPct = $s.Percent
            Write-Host ("  [{0,3}%] {1}  已用 {2}{3}" -f $s.Percent, $s.Label, (Format-Duration $s.ElapsedSec), $eta)
        }
    }
}

# ── 下载进度（pip 的 `--progress-bar raw` 输出 "Progress 已下 of 总大小"；curl 的 `--progress-bar` 输出百分比）─────
# 下载阶段的总量 Est 只是估算（pip 事先不告诉你要下几个文件、多大），所以进度封顶 99%，阶段结束时由 Exit-ProgressStage 补满。
$script:Dl = @{ Est = 1.0; Done = 0.0; Cur = 0.0; Tot = 0.0 }

function Reset-DownloadProgress {
    param([double]$EstBytes)
    $script:Dl = @{ Est = [Math]::Max($EstBytes, 1.0); Done = 0.0; Cur = 0.0; Tot = 0.0 }
}

function Complete-DownloadFile {
    $script:Dl.Done += $script:Dl.Tot
    $script:Dl.Cur = 0.0
    $script:Dl.Tot = 0.0
}

function Enter-DownloadFile {
    param([double]$Bytes)
    if ($script:Dl.Tot -gt 0) { Complete-DownloadFile }
    $script:Dl.Tot = [Math]::Max($Bytes, 1.0)
    $script:Dl.Cur = 0.0
}

# 认得出是进度行就更新进度并返回 $true（这一行不必再打进日志）；否则返回 $false。
function Update-DownloadProgressFromLine {
    param([string]$Line)
    if ($Line -match '^Progress (\d+) of (\d+)$') {
        $cur = [double]$Matches[1]
        $tot = [double]$Matches[2]
        if ($cur -eq 0) { Enter-DownloadFile -Bytes $tot }   # pip 每个文件都从 "Progress 0 of N" 开始
        $script:Dl.Cur = $cur
    }
    elseif ($script:Dl.Tot -gt 0 -and $Line -match '(\d+(?:\.\d+)?)%\s*$') {
        $script:Dl.Cur = [double]$Matches[1] / 100.0 * $script:Dl.Tot
    }
    else { return $false }
    $got = $script:Dl.Done + $script:Dl.Cur
    Set-ProgressFraction -Fraction ([Math]::Min(0.99, $got / $script:Dl.Est)) -Detail ("已下载 " + (Format-Bytes ([long]$got)))
    return $true
}

function Invoke-ExternalLogged {
    # 返回退出码；特殊值：-1 ＝ 超过总时限被杀掉，-2 ＝ 太久没有任何输出（卡死）被杀掉。用户点了「取消」则抛「已取消。」。
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSec = 900,
        [int]$StallSec = 0,
        [scriptblock]$LineFilter = $null,   # 每行输出先过它：返回 $true ＝ 这一行是进度信息、已经处理了，不打进日志
        [scriptblock]$Log = { param($m) Write-Host $m },
        [string]$Indent = "    ",
        [hashtable]$EnvVars = @{}
    )
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    $EnvVars = $EnvVars.Clone()
    if (-not $EnvVars.ContainsKey("PYTHONUTF8")) { $EnvVars["PYTHONUTF8"] = "1" }   # pip／python 的输出统一 UTF-8，日志里不出乱码
    $saved = @{}
    foreach ($k in $EnvVars.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
        [Environment]::SetEnvironmentVariable($k, [string]$EnvVars[$k], "Process")
    }
    try {
        $argLine = ($Arguments | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }) -join ' '
        $p = Start-Process -FilePath $FilePath -ArgumentList $argLine -NoNewWindow -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        $null = $p.Handle   # 5.1 的老毛病：不先取一下 Handle，进程退出后 ExitCode 会是空的
        $offsets = @{ $outFile = 0L; $errFile = 0L }
        $pending = @{ $outFile = ""; $errFile = "" }
        # 把两个重定向文件里新长出来的部分按行处理；返回这次处理了几行（进度行也算——它说明进程还活着）。
        $emit = {
            param([bool]$Final)
            $count = 0
            foreach ($f in @($outFile, $errFile)) {
                $fs = $null
                try {
                    $fs = [System.IO.File]::Open($f, "Open", "Read", "ReadWrite")
                    if ($fs.Length -gt $offsets[$f]) {
                        [void]$fs.Seek($offsets[$f], "Begin")
                        $buf = New-Object byte[] ($fs.Length - $offsets[$f])
                        $n = $fs.Read($buf, 0, $buf.Length)
                        $offsets[$f] += $n
                        $pending[$f] += [System.Text.Encoding]::UTF8.GetString($buf, 0, $n)
                    }
                } catch { } finally { if ($fs) { $fs.Dispose() } }
                $parts = ($pending[$f] -replace "`r`n", "`n" -replace "`r", "`n") -split "`n"
                if ($Final) { $pending[$f] = ""; $upto = $parts.Count } else { $pending[$f] = $parts[$parts.Count - 1]; $upto = $parts.Count - 1 }
                for ($i = 0; $i -lt $upto; $i++) {
                    $line = $parts[$i].TrimEnd()
                    if (-not $line) { continue }
                    $count++
                    $consumed = $false
                    if ($LineFilter) { $consumed = [bool](& $LineFilter $line) }
                    if (-not $consumed) { $null = & $Log ($Indent + $line) }
                }
            }
            return $count
        }
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $lastOut = 0
        $lastBeat = 0
        while (-not $p.WaitForExit(300)) {
            $lines = & $emit $false
            Update-GuiIfActive
            $sec = [int]$sw.Elapsed.TotalSeconds
            if ($lines -gt 0) { $lastOut = $sec }
            if ($script:CancelRequested) {
                & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null
                throw "已取消。"
            }
            if ($sec -ge $TimeoutSec) {
                [void](& $emit $true)
                & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null
                $null = & $Log ($Indent + "……超过 " + $TimeoutSec + " 秒还没跑完，已终止。多半是网络或代理卡住了（换个网络、或先关掉代理再试）。")
                return -1
            }
            if ($StallSec -gt 0 -and ($sec - $lastOut) -ge $StallSec) {
                [void](& $emit $true)
                & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null
                $null = & $Log ($Indent + "……" + $StallSec + " 秒没有任何进展，已终止。多半是网络或代理卡住了（换个网络、或先关掉代理再试）。")
                return -2
            }
            if (($sec - [Math]::Max($lastOut, $lastBeat)) -ge 30) {
                $lastBeat = $sec
                $null = & $Log ($Indent + "…还在跑（已 " + $sec + " 秒，最近 30 秒没有新输出）")
            }
        }
        $p.WaitForExit()
        [void](& $emit $true)
        return $p.ExitCode
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], "Process") }
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

# 各组件的 install.ps1 里写死了建 venv 用的 Python（缺省 3.12）。离线依赖的轮子必须按它来挑——
# 不能按"运行 pip 的那个 Python"挑：这台机器的系统 Python 是 3.13，那样下出来的全是 cp313 的轮子，
# 3.12 的 venv 装不上（实测：pip 暂存目录里躺着 cffi-…-cp313-cp313-win_amd64.whl）。
function Get-TargetPythonVersion {
    param($Comp)
    $inst = Join-Path $Comp.Dir "scripts\install.ps1"
    if (Test-Path -LiteralPath $inst) {
        $m = [regex]::Match((Get-Content -LiteralPath $inst -Raw -Encoding UTF8), '\$PythonVersion\s*=\s*"(\d+\.\d+)"')
        if ($m.Success) { return $m.Groups[1].Value }
    }
    return "3.12"
}

# 系统 pip 只负责"下载本机没有的那几个"（本机有的走下面的 Export-LocalWheels，根本不下）。
function Invoke-PipDownload {
    param(
        [string[]]$Specs,
        [string]$DestDir,
        [string]$IndexUrl = "",
        [string]$PythonVersion = "3.12",
        [switch]$NoDeps,
        [int]$TimeoutSec = 900,
        [scriptblock]$Log = { param($m) Write-Host $m }
    )
    if (-not $Specs -or $Specs.Count -eq 0) { return }
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    $pip = Find-PipExe
    if ($pip) { $exe = $pip; $pre = @() }
    else {
        $py = Find-PythonExe
        if (-not $py) { throw "下载依赖要用 pip——这台机器上找不到 pip 也找不到 python。" }
        $exe = $py; $pre = @("-m", "pip")
    }
    # 指定目标 Python／平台，pip 才会按它挑轮子；这几个参数要求 --only-binary=:all:。
    # --progress-bar raw：重定向时输出 "Progress 已下 of 总大小" 这种能解析的行，进度条靠它。
    $pipArgs = $pre + @("download") + $Specs + @(
        "-d", $DestDir, "--only-binary=:all:", "--python-version", $PythonVersion,
        "--implementation", "cp", "--platform", "win_amd64", "--progress-bar", "raw",
        "--timeout", "30", "--retries", "2", "--disable-pip-version-check", "--no-input")
    if ($NoDeps) { $pipArgs += "--no-deps" }
    if ($IndexUrl) { $pipArgs += @("--index-url", $IndexUrl) }
    # StallSec：正常下载时 pip 会不停地打进度行；240 秒一行都没有＝卡死（代理挂住），别再干等（实测踩过：一次卡了十几分钟）。
    $code = Invoke-ExternalLogged -FilePath $exe -Arguments $pipArgs -TimeoutSec $TimeoutSec -StallSec 240 -Log $Log `
        -LineFilter { param($l) Update-DownloadProgressFromLine -Line $l }
    if ($script:Dl.Tot -gt 0) { Complete-DownloadFile }
    if ($code -ne 0) { throw ("下载依赖库失败（pip 退出码 " + $code + "）：" + ($Specs -join ", ")) }
}

# 取远端文件大小（HEAD 请求的 Content-Length）；取不到就回 0，调用方自己兜底。
function Get-RemoteSize {
    param([string]$Url)
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { return 0L }
    $size = 0L
    try {
        foreach ($line in (& $curl.Source -sIL --max-time 25 $Url 2>$null)) {
            if ($line -match '^Content-Length:\s*(\d+)') { $size = [long]$Matches[1] }   # 跟随重定向时取最后一个
        }
    } catch { }
    return $size
}

# 直链下载（ROCm 那几个文件用）：连不上 20 秒放弃、速度低于 10 KB/s 持续 60 秒就放弃、180 秒没有任何输出也放弃——不会无限期挂着。
function Invoke-CurlDownload {
    param([string]$Url, [string]$OutFile, [long]$SizeBytes = 0, [int]$TimeoutSec = 3600, [scriptblock]$Log = { param($m) Write-Host $m })
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { throw "下载要用 curl.exe（Windows 10 1803 起自带），这台机器上找不到。" }
    $part = $OutFile + ".part"
    if ($SizeBytes -le 0) { $SizeBytes = 100MB }   # 不知道多大：按 100 MB 算，进度只是不那么准
    Enter-DownloadFile -Bytes $SizeBytes
    $code = Invoke-ExternalLogged -FilePath $curl.Source -TimeoutSec $TimeoutSec -StallSec 180 -Log $Log `
        -LineFilter { param($l) Update-DownloadProgressFromLine -Line $l } -Arguments @(
        "-L", "--fail", "--progress-bar", "--show-error", "--retry", "3", "--retry-delay", "2",
        "--connect-timeout", "20", "--speed-limit", "10240", "--speed-time", "60", "-o", $part, $Url)
    Complete-DownloadFile
    if ($code -ne 0) {
        Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
        throw ("下载失败（curl 退出码 " + $code + "）：" + $Url)
    }
    Move-Item -LiteralPath $part -Destination $OutFile -Force
}

# AMD 的 ROCm on Windows：几个直链（不是 index），版本号与 asr-shim\scripts\install.ps1 里的一致。
function Get-RocmDownloads {
    param($Comp)
    $rel = $Comp.Rocm.Release
    $tv = $Comp.Rocm.TorchVersion
    $inst = Get-Content -LiteralPath (Join-Path $Comp.Dir "scripts\install.ps1") -Raw -Encoding UTF8
    if ($inst -notmatch ('\$RocmRelease\s*=\s*"' + [regex]::Escape($rel) + '"') -or $inst -notmatch ('torch-' + [regex]::Escape($tv) + '%2Brocm')) {
        throw ("asr-shim 的 install.ps1 里 ROCm／torch 的版本变了，而 package-bundle.ps1 里的 Rocm 表还是 " + $rel + "／" + $tv +
            "——先把这张表改成和 install.ps1 一致，不然会悄悄打出一个错版本的包。")
    }
    $base = "https://repo.radeon.com/rocm/windows/rocm-rel-${rel}/"
    return @(
        ($base + "rocm_sdk_core-${rel}-py3-none-win_amd64.whl"),
        ($base + "rocm_sdk_devel-${rel}-py3-none-win_amd64.whl"),
        ($base + "rocm_sdk_libraries_custom-${rel}-py3-none-win_amd64.whl"),
        ($base + "rocm-${rel}.tar.gz"),
        ($base + "torch-${tv}%2Brocm${rel}-cp312-cp312-win_amd64.whl")
    )
}

# ── 依赖库打包计划：本机 venv 里现成的优先，缺的才下 ──────────────────────────────────────────
# 用组件自己 venv 的 python 跑 tools\localwheels.py plan：它列出本机装了什么、每条要求本机满不满足、
# 本机的 torch 是哪种构建（+cu128／+cpu／+rocm）。没有 venv（没跑过 install.ps1）就只能全部现下。
function Get-DepsPlan {
    # -Sizes：连每个包还原后的字节数一起算（要 stat 每个文件，torch 上万个，较慢）——只有真要打包、需要进度分母时才要；
    # 界面上随手切换显卡类型时不带它。
    param($Comp, [string]$GpuKey = "", [switch]$Sizes)
    $helper = Join-Path $script:ToolsDir "localwheels.py"
    $venvPy = Join-Path $Comp.Dir ".venv\Scripts\python.exe"
    $specs = @(@($Comp.BuildDeps) + @($Comp.PyDeps) | Where-Object { $_ })
    $info = $null
    if (Test-Path -LiteralPath $venvPy) {
        $argv = @($helper, "plan")
        if ($Sizes) { $argv += "--sizes" }
        foreach ($s in $specs) { $argv += @("--spec", $s) }
        try {
            $json = & $venvPy @argv 2>$null
            if ($LASTEXITCODE -eq 0 -and $json) { $info = ($json | Out-String) | ConvertFrom-Json }
        } catch { $info = $null }
    }
    $plan = [ordered]@{
        HaveVenv     = [bool]$info
        PyTag        = ""
        LocalTorch   = ""
        LocalCount   = 0
        RawBytes     = 0L
        MissingSpecs = @($specs)
        GpuKey       = $GpuKey
        TorchLocal   = $false
        TorchNeeded  = $false
    }
    if ($info) {
        $plan.PyTag = [string]$info.pyTag
        $plan.LocalTorch = [string]$info.torchVariant
        $plan.MissingSpecs = @($info.specs | Where-Object { $_.status -eq "missing" } | ForEach-Object { $_.spec })
    }
    if ($Comp.GpuTorch -and $GpuKey) {
        $plan.TorchLocal = [bool]($info -and $info.torchVariant -eq $GpuKey)
        $plan.TorchNeeded = -not $plan.TorchLocal
    }
    if ($info) {
        $skipTorch = $plan.TorchNeeded
        $usable = @($info.packages | Where-Object { -not $_.skip -and -not ($skipTorch -and $_.name -eq "torch") })
        $plan.LocalCount = $usable.Count
        if ($Sizes) {
            $sum = ($usable | Measure-Object -Property bytes -Sum).Sum
            if ($sum) { $plan.RawBytes = [long]$sum }
        }
    }
    return [pscustomobject]$plan
}

# 给界面和日志用的几句人话：本机有多少、缺什么要下。
function Format-DepsPlan {
    param($Plan, $Comp)
    $lines = @()
    if ($Plan.HaveVenv) {
        $lines += ("本机 venv 里已有 " + $Plan.LocalCount + " 个包（Python " + $Plan.PyTag + "）——直接还原成轮子打进去，不用下载。")
    } else {
        $lines += "这个组件在本机还没有 venv（没跑过 install.ps1）——依赖只能全部现下。"
    }
    $need = @($Plan.MissingSpecs)
    if ($Plan.TorchNeeded) {
        $g = $Comp.GpuTorch[$Plan.GpuKey]
        $need += ("PyTorch（" + $g.Label + "，" + $g.Approx + "）")
    }
    if ($need.Count -gt 0) { $lines += ("需要联网下载：" + ($need -join "、")) }
    else { $lines += "没有需要下载的——全部用本机现成的。" }
    return $lines
}

# 要下载的东西大概多大（字节）：给进度条的下载阶段当分母。pip 事先不告诉你要下几个文件、多大，所以只是估算。
function Get-DownloadEstimate {
    param($Comp, $Plan)
    $est = 0.0
    $est += 10MB * @($Plan.MissingSpecs).Count
    if (-not $Plan.HaveVenv) { $est += 150MB }   # 没有本机 venv：连传递依赖一起下
    if ($Plan.TorchNeeded) { $est += [double]$Comp.GpuTorch[$Plan.GpuKey].Bytes }
    return $est
}

# 把本机 venv 里已装的包还原成轮子（不联网）。torch 换了别的构建时不带本机这份。
# localwheels.py 会每 ~0.4 秒打一行 "PROG 已写 总量"，据此推进进度条（torch 一个包就要写好几分钟，不能一声不吭）。
function Export-LocalWheels {
    param($Comp, [string]$WheelsDir, [string[]]$Exclude = @(), [scriptblock]$Log = { param($m) Write-Host $m })
    $helper = Join-Path $script:ToolsDir "localwheels.py"
    $venvPy = Join-Path $Comp.Dir ".venv\Scripts\python.exe"
    New-Item -ItemType Directory -Force -Path $WheelsDir | Out-Null
    $argv = @($helper, "export", "--out", $WheelsDir)
    if ($Exclude.Count -gt 0) { $argv += @("--exclude", ($Exclude -join ",")) }
    $filter = {
        param($l)
        if ($l -match '^PROG (\d+) (\d+)$') {
            $total = [double]$Matches[2]
            if ($total -gt 0) {
                Set-ProgressFraction -Fraction ([double]$Matches[1] / $total) `
                    -Detail ("还原 " + (Format-Bytes ([long]$Matches[1])) + " / " + (Format-Bytes ([long]$total)))
            }
            return $true
        }
        return $false
    }
    $code = Invoke-ExternalLogged -FilePath $venvPy -Arguments $argv -TimeoutSec 3600 -StallSec 300 -Log $Log -LineFilter $filter
    if ($code -ne 0) { throw ("还原本机已装的包失败（localwheels.py export 退出码 " + $code + "，见上面 FAIL 那几行）。") }
}

# 本机没有的才下：普通依赖走 pip（按目标 Python 挑）；PyTorch 按选的显卡类型走 index 或 AMD 直链。
function Save-MissingDeps {
    param($Comp, $Plan, [string]$WheelsDir, [string]$PyVer, [scriptblock]$Log = { param($m) Write-Host $m })
    $isRocm = $false
    $rocmUrls = @()
    $rocmSizes = @()
    $est = Get-DownloadEstimate -Comp $Comp -Plan $Plan
    if ($Plan.TorchNeeded) {
        $g = $Comp.GpuTorch[$Plan.GpuKey]
        $isRocm = ($g.ContainsKey("Rocm") -and $g.Rocm)
        if ($isRocm) {
            $rocmUrls = @(Get-RocmDownloads -Comp $Comp)
            foreach ($u in $rocmUrls) { $rocmSizes += (Get-RemoteSize -Url $u) }   # 真实大小比表里的估算准
            $known = ($rocmSizes | Measure-Object -Sum).Sum
            if ($known -gt 0) { $est = $est - [double]$g.Bytes + [double]$known }
        }
    }
    Reset-DownloadProgress -EstBytes $est

    if (@($Plan.MissingSpecs).Count -gt 0) {
        & $Log ("  下载本机没有的依赖：" + (@($Plan.MissingSpecs) -join "、"))
        Invoke-PipDownload -Specs @($Plan.MissingSpecs) -DestDir $WheelsDir -PythonVersion $PyVer -TimeoutSec 900 -Log $Log
    }
    if ($Plan.TorchNeeded) {
        $g = $Comp.GpuTorch[$Plan.GpuKey]
        if ($isRocm) {
            $rocmDir = Join-Path $WheelsDir "rocm"
            New-Item -ItemType Directory -Force -Path $rocmDir | Out-Null
            & $Log ("  下载 AMD ROCm 的 PyTorch 与 SDK（" + (Format-Bytes ([long]$est)) + "，直链，未经真机验证）……")
            for ($i = 0; $i -lt $rocmUrls.Count; $i++) {
                $url = $rocmUrls[$i]
                $leaf = [System.Uri]::UnescapeDataString($url.Substring($url.LastIndexOf('/') + 1))
                $dst = Join-Path $rocmDir $leaf
                if (Test-Path -LiteralPath $dst) { & $Log ("    已有 " + $leaf); Enter-DownloadFile -Bytes $rocmSizes[$i]; Complete-DownloadFile; continue }
                & $Log ("    " + $leaf)
                Invoke-CurlDownload -Url $url -OutFile $dst -SizeBytes $rocmSizes[$i] -Log $Log
            }
            if (-not $Plan.HaveVenv) {
                # 本机没有 venv：ROCm 版 torch 自己的依赖（filelock、sympy…）也得一并下。
                $torchWhl = Get-ChildItem -LiteralPath $rocmDir -Filter "torch-*.whl" | Select-Object -First 1
                if ($torchWhl) { Invoke-PipDownload -Specs @($torchWhl.FullName) -DestDir $WheelsDir -PythonVersion $PyVer -TimeoutSec 900 -Log $Log }
            }
        } else {
            & $Log ("  下载 PyTorch（" + $g.Label + "，" + $g.Approx + "）……这一步可能要几分钟")
            # 本机有 venv 时其余依赖已经在上面还原好了，这里只要 torch 本体；没有 venv 就连它的依赖一起下。
            Invoke-PipDownload -Specs @("torch") -DestDir $WheelsDir -IndexUrl $g.IndexUrl -PythonVersion $PyVer `
                -NoDeps:$Plan.HaveVenv -TimeoutSec 3600 -Log $Log
        }
    }
}

# 离线自检：在一个全新的临时 Python 环境里，按目标机器 setup.ps1 的办法（UV_OFFLINE＋UV_FIND_LINKS）
# 演练一遍装环境。关键是给它一个【空的 uv 缓存】——否则在这台装过一堆东西的机器上，缺的包会被缓存悄悄补上，
# 自检永远通过（实测踩过：缺 setuptools 时 `-e` 装项目本体照样成功，就是因为 uv 缓存里有）。
function Test-OfflineWheels {
    param($Comp, $Plan, [string]$WheelsDir, [string]$PyVer, [scriptblock]$Log = { param($m) Write-Host $m })
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) { & $Log "    （没找到 uv，跳过离线自检）"; return }
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("rtb-verify-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        $venv = Join-Path $tmp "v"
        $vpy = Join-Path $venv "Scripts\python.exe"
        Set-ProgressFraction -Fraction 0.05 -Detail "建临时环境"
        $code = Invoke-ExternalLogged -FilePath $uv.Source -Arguments @("venv", "--python", $PyVer, $venv) -TimeoutSec 300 -Log $Log
        if ($code -ne 0) { throw ("离线自检建不出 Python " + $PyVer + " 的临时环境（uv venv 退出码 " + $code + "）") }
        $offEnv = @{ UV_OFFLINE = "1"; UV_FIND_LINKS = $WheelsDir; UV_CACHE_DIR = (Join-Path $tmp "uvcache"); UV_PYTHON_DOWNLOADS = "never" }

        # ① 依赖凑不凑得齐：dry-run，不真装（torch 好几个 GB，没必要在临时目录里再装一遍）。
        Set-ProgressFraction -Fraction 0.2 -Detail "演练：依赖凑不凑得齐"
        $reqs = @(@($Comp.PyDeps) | Where-Object { $_ })
        $depArgs = @("pip", "install", "--dry-run", "--python", $vpy)
        if ($Comp.GpuTorch -and $Plan.GpuKey) {
            $g = $Comp.GpuTorch[$Plan.GpuKey]
            if ($g.ContainsKey("Rocm") -and $g.Rocm) {
                $depArgs += @(Get-ChildItem -LiteralPath (Join-Path $WheelsDir "rocm") -File | Where-Object { $_.Name -match '^(rocm_sdk_.*\.whl|rocm-.*\.tar\.gz|torch-.*\.whl)$' } | ForEach-Object { $_.FullName })
            } else {
                $depArgs += @("--index-url", $g.IndexUrl, "torch")
            }
        }
        $depArgs += $reqs
        if ($depArgs.Count -gt 5) {
            $code = Invoke-ExternalLogged -FilePath $uv.Source -Arguments $depArgs -EnvVars $offEnv -TimeoutSec 600 -StallSec 300 -Log $Log
            if ($code -ne 0) { throw "离线演练：依赖凑不齐（见上面 uv 的报错）——包里缺了轮子，目标机器离线装会失败。" }
        }

        # ② 项目本体能不能装：install.ps1 里的 `uv pip install -e` 要在隔离环境里现拉 build 依赖（setuptools）。
        #    在源码副本上做，构建会往源码目录写 egg-info，别弄脏打包用的那份。
        Set-ProgressFraction -Fraction 0.6 -Detail "演练：装项目本体"
        Test-CancelRequested
        $src = Join-Path $tmp "src"
        robocopy.exe $Comp.Dir $src /E /XD ".venv*" "__pycache__" "models" "samples" "tests" ".offline-wheels" "*.egg-info" /XF "*.pyc" /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        if ($LASTEXITCODE -ge 8) { throw ("离线自检复制源码失败（robocopy 退出码 " + $LASTEXITCODE + "）") }
        $code = Invoke-ExternalLogged -FilePath $uv.Source -Arguments @("pip", "install", "--python", $vpy, "--no-deps", "-e", $src) -EnvVars $offEnv -TimeoutSec 600 -StallSec 300 -Log $Log
        if ($code -ne 0) { throw "离线演练：装不了项目本体（多半是缺 build 依赖 setuptools）——包里缺了轮子，目标机器离线装会失败。" }
    } finally {
        Remove-DirLongPathSafe -Path $tmp
    }
}

# 复制一个目录（模型），进度条按【目标目录已经长了多少字节】推进。robocopy 自带长路径支持（模型文件名动辄很长，
# 实测踩过：279 字符就让 Copy-Item -Recurse 中途报“找不到路径”），所以仍用它干活，只是改成异步起、边等边量。
function Copy-DirWithProgress {
    param([string]$Source, [string]$Destination, [long]$TotalBytes = 0, [string]$Detail = "")
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    if ($TotalBytes -le 0) { $TotalBytes = Get-DirSize $Source }
    $argLine = '"' + $Source + '" "' + $Destination + '" /E /NFL /NDL /NJH /NJS /NC /NS /NP'
    $p = Start-Process -FilePath "robocopy.exe" -ArgumentList $argLine -NoNewWindow -PassThru
    $null = $p.Handle
    while (-not $p.WaitForExit(250)) {
        if ($script:CancelRequested) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            throw "已取消。"
        }
        $done = Get-DirSize $Destination
        if ($TotalBytes -gt 0) {
            Set-ProgressFraction -Fraction ($done / $TotalBytes) -Detail ($Detail + "  " + (Format-Bytes $done) + " / " + (Format-Bytes $TotalBytes))
        }
        Update-GuiIfActive
    }
    $p.WaitForExit()
    if ($p.ExitCode -ge 8) { throw ("复制模型目录失败（robocopy 退出码 " + $p.ExitCode + "）：" + $Source) }
}

# 一个字节都不该丢：压完后把 zip 里的文件数、总字节数与源目录对一遍，对不上就当失败（宁可当场报错，
# 也别让人拿着一个缺东西的包到新机器上才发现）。
function Test-ZipMatchesSource {
    param([string]$ZipPath, [int]$ExpectFiles, [long]$ExpectBytes)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $za = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $n = $za.Entries.Count
        $sum = 0L
        foreach ($e in $za.Entries) { $sum += $e.Length }
    } finally {
        $za.Dispose()
    }
    if ($n -ne $ExpectFiles -or $sum -ne $ExpectBytes) {
        throw ("压缩包校验没过：应有 " + $ExpectFiles + " 个文件、" + $ExpectBytes + " 字节，实际 " + $n + " 个、" + $sum + " 字节。")
    }
}

# 把常见的失败原因翻成人话（以前一律说"多半是路径太长"，遇到 2 GB 限制、磁盘满时把人往错误的方向引）。
function Get-FriendlyPackError {
    param([string]$Message)
    if ($Message -match '已取消') { return "已取消。" }
    if ($Message -match 'not enough space|no space left|磁盘空间不足|ERROR_DISK_FULL|There is not enough space') {
        return "磁盘空间不够了——清理一下输出目录所在的盘和 C 盘临时目录（%TEMP%）后再试。"
    }
    if ($Message -match 'Stream was too long') {
        return "单个文件超过了压缩组件的 2 GB 上限（旧版 Compress-Archive 的限制）——本脚本已改用 ZipArchive，看到这句说明跑的是旧版脚本，请更新。"
    }
    if ($Message -match 'PathTooLong|path.*too long|路径.*太长|filename.*too long') {
        return "路径太长（Windows 经典上限 260 字符）——到系统设置搜「启用 Win32 长路径」打开，或把输出目录换到浅一点的地方。"
    }
    if ($Message -match 'Access to the path|拒绝访问|being used by another process|另一个进程') {
        return "文件被占用或没有权限——关掉正在用这些文件的程序（杀毒软件的实时扫描也可能占着大文件），换个输出目录再试。"
    }
    return ""
}

# ── 压缩：不用 Compress-Archive ───────────────────────────────────────────────────────────────
# Compress-Archive 在 Windows PowerShell 5.1 里，单个文件超过 2 GB 就报 "Stream was too long"（实测：2.3 GB 的文件必挂），
# 而 Qwen3-ASR-1.7B 的 model.safetensors 就有 4 GB，CUDA 版 PyTorch 的轮子也快 3 GB。直接用 .NET 的
# ZipArchive 流式写就没有这个限制。轮子、权重这类已经压过的东西用"仅存储"，不白费时间再压一遍。
# 进度按【已写入的字节数 ÷ 总字节数】推进；返回 文件数／总字节数，供事后校验。
function New-ZipFromDirectory {
    param([string]$SourceDir, [string]$ZipPath, [scriptblock]$Log = { param($m) Write-Host $m })
    Add-Type -AssemblyName System.IO.Compression
    $SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path.TrimEnd('\')
    $storeExt = @(".whl", ".zip", ".gz", ".bz2", ".xz", ".7z", ".safetensors", ".onnx")
    $files = @(Get-ChildItem -LiteralPath $SourceDir -Recurse -File -Force)
    $total = 0L
    foreach ($f in $files) { $total += $f.Length }
    $written = 0L
    $fs = [System.IO.File]::Open($ZipPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::ReadWrite)
    $za = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create, $false)
    $buf = New-Object byte[] (4MB)
    try {
        foreach ($f in $files) {
            $rel = $f.FullName.Substring($SourceDir.Length + 1) -replace '\\', '/'
            $level = if ($storeExt -contains $f.Extension.ToLowerInvariant()) { [System.IO.Compression.CompressionLevel]::NoCompression } else { [System.IO.Compression.CompressionLevel]::Optimal }
            if ($f.Length -ge 200MB) { & $Log ("    压入 " + $rel + "（" + (Format-Bytes $f.Length) + "）……") }
            $entry = $za.CreateEntry($rel, $level)
            $mtime = $f.LastWriteTime
            if ($mtime.Year -lt 1980) { $mtime = [datetime]"1980-01-01" }
            $entry.LastWriteTime = $mtime
            $short = if ($rel.Length -gt 60) { "…" + $rel.Substring($rel.Length - 59) } else { $rel }
            $in = [System.IO.File]::OpenRead("\\?\" + $f.FullName)   # \\?\ 前缀：路径超过 260 字符也能开
            try {
                $out = $entry.Open()
                try {
                    while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                        $out.Write($buf, 0, $n)
                        $written += $n
                        if ($total -gt 0) {
                            Set-ProgressFraction -Fraction ($written / $total) -Detail ($short + "  " + (Format-Bytes $written) + " / " + (Format-Bytes $total))
                        }
                        Update-GuiIfActive
                        Test-CancelRequested
                    }
                } finally { $out.Dispose() }
            } finally { $in.Dispose() }
        }
    } finally {
        $za.Dispose()
        $fs.Dispose()
    }
    return [pscustomobject]@{ Files = $files.Count; Bytes = $total }
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
            GpuOrder          = if ($override -and $override.ContainsKey('GpuOrder')) { @($override.GpuOrder) } else { @() }
            Rocm              = if ($override -and $override.ContainsKey('Rocm')) { $override.Rocm } else { $null }
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

# ── 动手之前先把活儿算清楚：每个组件带哪些模型、要不要打依赖库、本机有多少/缺什么、各阶段权重 ────────────
# 这样：① 进度条一开始就有分母；② 预检（磁盘、网络）能在花几分钟做无用功之前就拦下来。
function Get-BundleWorkPlan {
    param($AllComponents, [string[]]$ComponentIds, [hashtable]$ModelSelection, [hashtable]$OfflineDeps)
    $byId = @{}
    foreach ($c in $AllComponents) { $byId[$c.Id] = $c }
    $work = [ordered]@{}
    foreach ($id in $ComponentIds) {
        $c = $byId[$id]
        $selection = $ModelSelection[$id]
        $models = @()
        if ($c.ModelKind -eq "sizes" -or $c.ModelKind -eq "generic") {
            $keys = @($selection)
            foreach ($choice in $c.SizeChoices) {
                if ($keys -contains $choice.Key) {
                    $models += [pscustomobject]@{ Name = (Split-Path -Leaf $choice.Dir); Dir = $choice.Dir; Bytes = [long]$choice.Bytes; Label = $choice.Label; Slot = "" }
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
                $models += [pscustomobject]@{ Name = $found.Name; Dir = $found.Dir; Bytes = [long]$found.Bytes; Label = $found.Name; Slot = $slot }
            }
        }
        $modelBytes = 0L
        foreach ($m in $models) { $modelBytes += $m.Bytes }

        $offSel = $OfflineDeps[$id]
        $gpuKey = ""
        $pyVer = ""
        $plan = $null
        $dlBytes = 0.0
        $rawBytes = 0L
        if ($offSel) {
            if ($c.GpuTorch) {
                if (($offSel -is [string]) -and $c.GpuTorch.ContainsKey($offSel)) { $gpuKey = $offSel }
                else { throw ("组件 " + $id + " 要指定显卡类型：" + ($c.GpuOrder -join "／") + "（收到的是：" + $offSel + "）") }
            }
            $pyVer = Get-TargetPythonVersion -Comp $c
            $plan = Get-DepsPlan -Comp $c -GpuKey $gpuKey -Sizes
            $dlBytes = Get-DownloadEstimate -Comp $c -Plan $plan
            $rawBytes = $plan.RawBytes
        }
        $work[$id] = [pscustomobject]@{
            Comp = $c; Selection = $selection; Models = $models; ModelBytes = $modelBytes
            Offline = [bool]$offSel; GpuKey = $gpuKey; PyVer = $pyVer; Plan = $plan
            RawBytes = [long]$rawBytes; DownloadBytes = [double]$dlBytes
        }
    }
    return $work
}

# 各阶段的估算耗时（秒），只用来分配进度条的比例与给 ETA 一个起点——实际速度会在运行中校准 ETA。
# 依据这台机器上实测的量级（asr-shim 全套 7.9 GB 打一次 4 分钟）：复制模型 ≈ 180 MB/s；还原轮子（要 deflate 压缩）
# ≈ 30 MB/s；走代理下载 ≈ 2 MB/s；压缩成 zip（轮子、权重都是"仅存储"）≈ 200 MB/s；轮子压缩后约为原始大小的一半多。
function Get-WorkWeights {
    param($W)
    $wheelBytes = [double]$W.RawBytes * 0.55 + [double]$W.DownloadBytes
    $stageBytes = [double]$W.Comp.SourceBytes + [double]$W.ModelBytes + $wheelBytes
    return [pscustomobject]@{
        Source   = 3.0
        Models   = [double]$W.ModelBytes / 180MB
        Export   = if ($W.Offline -and $W.Plan.HaveVenv) { [double]$W.RawBytes / 30MB } else { 0.0 }
        Download = [double]$W.DownloadBytes / 2MB
        Verify   = if ($W.Offline) { 12.0 } else { 0.0 }
        StageBytes = $stageBytes
    }
}

# 用 curl 探一下某个地址通不通（走系统代理环境变量，和 pip 一样）。返回 HTTP 状态码，0 ＝ 连不上。
function Test-UrlReachable {
    param([string]$Url, [int]$TimeoutSec = 30)
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { return -1 }
    $out = & $curl.Source -s -o NUL -I -L --max-time $TimeoutSec -w "%{http_code}" $Url 2>$null
    $code = 0
    [void][int]::TryParse(([string]$out).Trim(), [ref]$code)
    return $code
}

function Get-FreeBytes {
    param([string]$Path)
    try { return (New-Object System.IO.DriveInfo ([System.IO.Path]::GetPathRoot($Path))).AvailableFreeSpace } catch { return [long]::MaxValue }
}

# 预检：在花几分钟做无用功之前，把"注定会失败"的情况拦下来，并且说清楚怎么办。
#   ① 磁盘空间：暂存目录（%TEMP%）放一整套要打包的内容；zip 直接写到输出目录（写完改名，省得跨盘再搬一遍）。
#   ② 需要联网下载时先探网络：以前 pip 走一个挂住的代理，什么都不说地卡了十几分钟才有结果。
#   ③ 输出目录能不能写。
function Test-PackagePreflight {
    param($Work, [string]$OutputDir, [scriptblock]$Log = { param($m) Write-Host $m })
    $stageBytes = 0.0
    $needNet = $false
    $hosts = @{}
    foreach ($k in $Work.Keys) {
        $w = $Work[$k]
        $stageBytes += (Get-WorkWeights $w).StageBytes
        if ($w.Offline) {
            $missing = @($w.Plan.MissingSpecs).Count -gt 0
            if ($missing -or $w.Plan.TorchNeeded) { $needNet = $true }
            if ($missing) { $hosts["https://pypi.org/simple/"] = "PyPI（pip 下载依赖）" }
            if ($w.Plan.TorchNeeded) {
                $g = $w.Comp.GpuTorch[$w.GpuKey]
                if ($g.ContainsKey("Rocm") -and $g.Rocm) { $hosts["https://repo.radeon.com/rocm/windows/"] = "AMD ROCm 下载站" }
                else { $hosts[$g.IndexUrl + "/"] = "PyTorch 下载站" }
            }
        }
    }
    $stageBytes = $stageBytes * 1.05 + 50MB
    $zipBytes = $stageBytes * 0.95

    $tmpRoot = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetTempPath())
    $outRoot = [System.IO.Path]::GetPathRoot($OutputDir)
    $needTmp = $stageBytes
    $needOut = $zipBytes
    if ($tmpRoot -eq $outRoot) { $needTmp += $zipBytes; $needOut = 0.0 }   # 同一个盘：两份都得放得下
    foreach ($pair in @(@($tmpRoot, $needTmp, "临时目录（%TEMP%）"), @($outRoot, $needOut, "输出目录"))) {
        if ($pair[1] -le 0) { continue }
        $free = Get-FreeBytes $pair[0]
        if ($free -lt $pair[1]) {
            throw ("磁盘空间不够：" + $pair[2] + "所在的 " + $pair[0] + " 约需 " + (Format-Bytes ([long]$pair[1])) + "，只剩 " + (Format-Bytes $free) + "。清理一下再试，或者少带一个模型。")
        }
    }
    & $Log ("  预检：暂存约 " + (Format-Bytes ([long]$stageBytes)) + "、成品约 " + (Format-Bytes ([long]$zipBytes)) + "，磁盘空间够。")

    # 输出目录可写
    $probe = Join-Path $OutputDir (".write-test-" + [guid]::NewGuid().ToString("N").Substring(0, 6))
    try { [System.IO.File]::WriteAllText($probe, "x"); Remove-Item -LiteralPath $probe -Force }
    catch { throw ("输出目录写不进去：" + $OutputDir + "（" + $_.Exception.Message + "）") }

    if ($needNet) {
        foreach ($url in $hosts.Keys) {
            & $Log ("  预检：探一下 " + $hosts[$url] + " 通不通……")
            $code = Test-UrlReachable -Url $url -TimeoutSec 30
            if ($code -lt 200 -or $code -ge 400) {
                $proxy = if ($env:HTTPS_PROXY) { $env:HTTPS_PROXY } elseif ($env:HTTP_PROXY) { $env:HTTP_PROXY } else { "（没设）" }
                throw ("连不上 " + $hosts[$url] + "（" + $url + "，HTTP " + $code + "），这次要下载的东西下不来。当前代理：" + $proxy +
                    "。换个网络／检查代理后再试；或者选「本机已装」的显卡类型、取消勾选「打包依赖库」，就不需要联网了。")
            }
        }
        & $Log "  预检：网络通。"
    }
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
        # GPU 变体 key（"nvidia"／"amd"／"cpu"）；没有 GpuTorch 表的组件，任何非空真值就够（比如 "1"）。
        [hashtable]$OfflineDeps = @{},
        [scriptblock]$Log = { param($m) Write-Host $m },
        # 进度回调：收到一个对象 { Percent, Fraction, Label, Detail, ElapsedSec, EtaSec }。不给就用命令行的 Write-Progress。
        [scriptblock]$Report = $null
    )
    $script:CancelRequested = $false
    if (-not $Report) { $Report = New-ConsoleReporter }

    $all = Get-ToolboxComponents -RepoRoot $RepoRoot
    $byId = @{}
    foreach ($c in $all) { $byId[$c.Id] = $c }

    $unknown = $ComponentIds | Where-Object { -not $byId.ContainsKey($_) }
    if ($unknown) {
        throw ("不认识这些组件 id：" + ($unknown -join ", ") + "。能打包的有：" + (($all | ForEach-Object { $_.Id }) -join ", "))
    }
    if ($ComponentIds.Count -eq 0) { throw "一个组件都没选，没什么可打包的。" }
    if ($ZipName -match '[\\/:*?"<>|]') { throw ("压缩包名里不能有这些字符：\ / : * ? "" < > |（收到：" + $ZipName + "）") }

    if (-not (Test-Path -LiteralPath $OutputDir)) { New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null }
    $OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path
    $zipPath = Join-Path $OutputDir ($ZipName + ".zip")
    $zipPart = $zipPath + ".part"   # 先写成 .part，全部写完、校验过再改名——中途失败/取消不会留下一个看着像样的残缺 zip，也不会覆盖掉旧的
    # 组装过程在这里发生，跟用户要 zip 最终落在哪个目录是两回事——固定放到临时目录下一个短随机名，
    # 路径深度就与用户选哪儿当 -OutputDir 无关了（模型目录自己的文件名就很长，再加上用户选的输出目录，
    # 实测踩过：279 字符就让 Copy-Item 中途报“找不到路径”）。
    $stageDir = Join-Path ([System.IO.Path]::GetTempPath()) ("rtb-" + [guid]::NewGuid().ToString("N").Substring(0, 10))
    if (Test-Path -LiteralPath $stageDir) { Remove-DirLongPathSafe -Path $stageDir }
    New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
    trap {
        # 打包中途失败或被取消：别把可能好几个 GB 的暂存目录、半截的 zip 留下，然后照常把错误往上抛。
        Remove-DirLongPathSafe -Path $stageDir
        Remove-Item -LiteralPath $zipPart -Force -ErrorAction SilentlyContinue
        break
    }

    # ── 先算清楚要干什么、各要多久，再预检，再开工 ──────────────────────────────────────────────
    & $Log "==> 分析要打包的内容……"
    $work = Get-BundleWorkPlan -AllComponents $all -ComponentIds $ComponentIds -ModelSelection $ModelSelection -OfflineDeps $OfflineDeps
    Test-PackagePreflight -Work $work -OutputDir $OutputDir -Log $Log

    $totalW = 5.0   # 写清单／README／setup.ps1、校验、收尾
    $stageBytesAll = 0.0
    foreach ($k in $work.Keys) {
        $wt = Get-WorkWeights $work[$k]
        $totalW += $wt.Source + $wt.Models + $wt.Export + $wt.Download + $wt.Verify
        $stageBytesAll += $wt.StageBytes
    }
    $zipWeight = [Math]::Max($stageBytesAll / 200MB, 1.0)
    Initialize-Progress -TotalWeight ($totalW + $zipWeight) -Report $Report

    $manifestComponents = @()
    $sizeTotal = 0L
    $uvBundled = $false   # uv.exe 只需要在 zip 里放一份，供所有带离线依赖的组件共用
    $swAll = [System.Diagnostics.Stopwatch]::StartNew()

    foreach ($id in $ComponentIds) {
        $w = $work[$id]
        $c = $w.Comp
        $wt = Get-WorkWeights $w
        & $Log ("`n==> [" + $id + "] " + $c.Name)
        $destDir = Join-Path $stageDir $id
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null

        Enter-ProgressStage -Label ($id + "：复制源码") -Weight $wt.Source
        $xd = @(".venv*", "__pycache__", "models", "samples", ".pytest_cache", ".mypy_cache", ".ruff_cache", "*.egg-info")
        if (-not $IncludeTests) { $xd += "tests" }
        $roboArgs = @($c.Dir, $destDir, "/E", "/XD") + $xd + @("/XF", "*.pyc", "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS")
        & robocopy.exe @roboArgs | Out-Null
        if ($LASTEXITCODE -ge 8) { throw ("复制 " + $id + " 的源码失败（robocopy 退出码 " + $LASTEXITCODE + "）") }
        & $Log ("  源码已复制（不含 .venv/models/tests 等，见上面排除表）")
        Exit-ProgressStage

        $bundledModelNames = @()
        foreach ($m in $w.Models) {
            Test-CancelRequested
            $dstModel = Join-Path $destDir (Join-Path "models" $m.Name)
            # 这一份模型占本组件"复制模型"阶段的多大一块，就分多大的权重
            $share = if ($w.ModelBytes -gt 0) { $wt.Models * ([double]$m.Bytes / [double]$w.ModelBytes) } else { 0.0 }
            Enter-ProgressStage -Label ($id + "：复制模型 " + $m.Name) -Weight $share
            Copy-DirWithProgress -Source $m.Dir -Destination $dstModel -TotalBytes $m.Bytes -Detail "复制模型"
            Exit-ProgressStage
            $bundledModelNames += $m.Name
            $tag = if ($m.Slot) { "（" + $m.Slot + "）" } else { "" }
            & $Log ("  已带上模型" + $tag + "：" + $m.Label + "（" + (Format-Bytes $m.Bytes) + "）")
        }

        $registerArgs = $null
        if ($bundledModelNames.Count -gt 0) {
            $sel = if ($c.ModelKind -eq "pair") { $w.Selection } else { @($w.Selection) }
            $registerArgs = & $c.BuildRegisterArgs $sel
        }
        if (-not $registerArgs) {
            if ($c.DownloadScriptRel) { & $Log ("  没带模型——目标机器会跑 " + $c.DownloadScriptRel + "（联网下载）") }
            else { & $Log ("  这个组件没有模型概念（或没找到下载脚本），目标机器只会跑 register（无参）") }
        }

        # 离线依赖：把这个组件的 wheel 也打进去，目标机器装环境这步就不用联网了（模型那部分本来就已经
        # 能离线——见上面；这里补的是 install.ps1 里 `uv pip install` 那几步）。setup.ps1 只认
        # 「这个组件目录下有没有 .offline-wheels」，不看 manifest 里的依赖细节——生成器与重放器各管各的。
        #
        # 【本机现成的优先，缺的才下】本机 venv 里装好、跑通过的那一套直接还原成轮子（tools\localwheels.py），
        # 只有本机没有的（比如换一种显卡的 PyTorch，或本机 venv 里没装的 build 依赖）才去下载。
        # 轮子按各组件 install.ps1 用的 Python（3.12）挑，而不是按运行本脚本的系统 Python（3.13 会下出 cp313 的轮子）。
        $hasOfflineDeps = $false
        $gpuVariant = ""
        if ($w.Offline) {
            $plan = $w.Plan
            $pyVer = $w.PyVer
            $wheelsDir = Join-Path $destDir ".offline-wheels"
            & $Log ("  打包依赖库（离线安装）——本机现成的优先，缺的才下载（目标 Python " + $pyVer + "）")
            foreach ($planLine in (Format-DepsPlan -Plan $plan -Comp $c)) { & $Log ("    " + $planLine) }

            if ($plan.HaveVenv) {
                Enter-ProgressStage -Label ($id + "：还原本机已装的包") -Weight $wt.Export
                & $Log "  还原本机已装的包（不联网）……"
                $exclude = @()
                if ($plan.TorchNeeded) { $exclude += "torch" }   # 选了别的构建，本机这份 torch 不带
                Export-LocalWheels -Comp $c -WheelsDir $wheelsDir -Exclude $exclude -Log $Log
                Exit-ProgressStage
            }
            if ($wt.Download -gt 0) {
                Enter-ProgressStage -Label ($id + "：下载本机没有的依赖") -Weight $wt.Download
                Save-MissingDeps -Comp $c -Plan $plan -WheelsDir $wheelsDir -PyVer $pyVer -Log $Log
                Exit-ProgressStage
            }

            $wheelCount = @(Get-ChildItem -LiteralPath $wheelsDir -Recurse -Filter "*.whl" -ErrorAction SilentlyContinue).Count
            & $Log ("  共 " + $wheelCount + " 个 wheel（" + (Format-Bytes (Get-DirSize $wheelsDir)) + "）")
            Enter-ProgressStage -Label ($id + "：离线自检") -Weight $wt.Verify
            & $Log "  离线自检：用空的 uv 缓存，在临时环境里按目标机器的办法演练一遍……"
            Test-OfflineWheels -Comp $c -Plan $plan -WheelsDir $wheelsDir -PyVer $pyVer -Log $Log
            & $Log "    自检通过——目标机器装环境这步不用联网了"
            Exit-ProgressStage
            $hasOfflineDeps = $true
            $gpuVariant = $w.GpuKey
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
            gpuVariant = $gpuVariant
            pythonVersion = $(if ($w.Offline) { $w.PyVer } else { "" })   # 带了离线依赖库才需要：目标机器要预先装好它
            registerArgs = $registerArgs
            downloadScriptRel = $c.DownloadScriptRel
            bytes = $destBytes
        }
    }

    Enter-ProgressStage -Label "写入安装清单与脚本" -Weight 2.0
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
    Exit-ProgressStage

    & $Log ("`n==> 压缩")
    # zip 直接写到输出目录里的 .part 文件（不再先写 %TEMP% 再跨盘搬——8 GB 的包搬一趟要好几分钟、还没有进度）。
    # 读的是这个短路径的暂存目录，路径深度与输出目录无关。
    Enter-ProgressStage -Label "压缩" -Weight $zipWeight
    try {
        # 不用 Compress-Archive：它在 5.1 里单个文件超过 2 GB 就报 "Stream was too long"（见 New-ZipFromDirectory 上的说明）。
        $zipInfo = New-ZipFromDirectory -SourceDir $stageDir -ZipPath $zipPart -Log $Log
    } catch {
        $raw = $_.Exception.Message
        Remove-Item -LiteralPath $zipPart -Force -ErrorAction SilentlyContinue
        $hint = Get-FriendlyPackError -Message $raw
        if ($hint -eq "已取消。") { throw "已取消。" }
        throw ("压缩失败。" + $(if ($hint) { $hint + " " } else { "" }) + "原始错误：" + $raw)
    }
    Exit-ProgressStage

    Enter-ProgressStage -Label "校验压缩包" -Weight 3.0
    Test-ZipMatchesSource -ZipPath $zipPart -ExpectFiles $zipInfo.Files -ExpectBytes $zipInfo.Bytes
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
    Move-Item -LiteralPath $zipPart -Destination $zipPath -Force   # 同一个盘上的改名，瞬间完成
    $zipBytes = (Get-Item -LiteralPath $zipPath).Length
    & $Log ("  " + $zipPath + "（" + (Format-Bytes $zipBytes) + "，" + $zipInfo.Files + " 个文件，校验通过）")
    Exit-ProgressStage

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
    Set-ProgressFraction -Fraction 1.0 -Detail "完成"
    Send-ProgressReport -Force
    & $Log ("  总用时 " + (Format-Duration $swAll.Elapsed.TotalSeconds))

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
    $lines += "  3. 等它跑完——每个组件会建自己的 Python 虚拟环境、装依赖（带了「依赖库」的组件这一步不联网，其余要联网），"
    $lines += "     然后向如意登记自己。"
    $lines += "  4. 重启如意工作台（或第一次启动），设置页「MCP／扩展组件」栏应该能看到新组件了。"
    $lines += ""
    $lines += "先决条件：机器上要能用 uv（https://astral.sh/uv）。没有的话 setup.ps1 会告诉你怎么装，"
    $lines += "或者直接用 -InstallUv 参数让它自动装：powershell -ExecutionPolicy Bypass -File .\setup.ps1 -InstallUv"
    $lines += ""
    foreach ($c in $Manifest.components) {
        $lines += ("· " + $c.name + "（" + $c.id + "）")
        if ($c.offlineDeps) {
            $lines += ("    已带上依赖库" + $(if ($c.gpuVariant) { "（PyTorch：" + $c.gpuVariant + "）" } else { "" }) + " —— 装环境这步不联网（目标机器要先装好 Python 3.12）。")
        }
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
# 先把这次要装的组件筛出来，总进度才有分母（-Only／-Skip 之后剩几个）。
$todo = @($manifest.components | Where-Object { ($Only.Count -eq 0 -or ($Only -contains $_.id)) -and ($Skip -notcontains $_.id) })
$compIdx = 0
foreach ($c in $todo) {
    $compIdx++
    Write-Progress -Id 1 -Activity "安装扩展组件" -PercentComplete ([int](100 * ($compIdx - 1) / $todo.Count)) `
        -Status ("[" + $compIdx + "/" + $todo.Count + "] " + $c.name)

    Write-Host ("`n==> [" + $compIdx + "/" + $todo.Count + "] " + $c.id + "：" + $c.name) -ForegroundColor Cyan
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
        # 离线模式下 uv 不会去联网下载 Python 解释器：机器上没有对应版本，install.ps1 里的 `uv venv` 只会甩一句
        # 难懂的 "No interpreter found ... uv is set to offline mode"。先查一下，缺就说人话、告诉怎么装。
        if ($c.pythonVersion) {
            $prevEap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            & uv python find $c.pythonVersion 2>$null | Out-Null
            $pyFound = ($LASTEXITCODE -eq 0)
            $ErrorActionPreference = $prevEap
            if (-not $pyFound) {
                Write-Host ("  这台机器上找不到 Python " + $c.pythonVersion + "。这个包带的是离线依赖库，装环境时不联网，uv 也就没法自己去下载 Python。") -ForegroundColor Red
                Write-Host ("  先装一个：能联网的话运行  uv python install " + $c.pythonVersion + "  ；或者从 https://www.python.org/downloads/ 装 Python " + $c.pythonVersion + "。")
                Write-Host "  装好之后重新双击「安装并接入如意.cmd」（已装好的组件会直接跳过建环境那步）。"
                $failCount++; continue
            }
        }
        $env:UV_OFFLINE = "1"
        $env:UV_FIND_LINKS = $offlineWheels
    }
    try {
        # 包里带的是哪种 PyTorch 就装哪种（manifest 的 gpuVariant），别让 install.ps1 按【目标机器】的显卡再猜一遍——
        # 带的是 NVIDIA 版、目标机器却是 AMD 卡，它就会转去联网下 ROCm 版，离线必然失败。
        if ($c.gpuVariant) { & $installScript -Gpu $c.gpuVariant }
        else { & $installScript }
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

Write-Progress -Id 1 -Activity "安装扩展组件" -Completed
Write-Host ""
if ($failCount -eq 0) {
    Write-Host ("全部 " + $okCount + " 个组件都装好并登记了。重启如意工作台（或第一次启动），设置页「MCP／扩展组件」栏应该能看到。") -ForegroundColor Green
} else {
    Write-Host ($okCount.ToString() + " 个成功，" + $failCount.ToString() + " 个没成功（见上面各自的原因）。") -ForegroundColor Yellow
}
exit $failCount
'@
}

# 界面上「依赖库」那块：勾上「打包依赖库」才显示；换显卡类型时重算（本机有多少个包、缺哪个要下）。
function Update-DepsPlanUi {
    param($Ctx)
    $on = [bool]$Ctx.Chk.Checked
    $Ctx.PlanLabel.Visible = $on
    if ($Ctx.Combo) { $Ctx.Combo.Enabled = $on }
    if (-not $on) { return }
    $gpuKey = ""
    if ($Ctx.Combo) { $gpuKey = [string]$Ctx.KeyList[[Math]::Max(0, $Ctx.Combo.SelectedIndex)] }
    $plan = Get-DepsPlan -Comp $Ctx.Comp -GpuKey $gpuKey
    $Ctx.PlanLabel.Text = (Format-DepsPlan -Plan $plan -Comp $Ctx.Comp) -join "`r`n"
}

# ── 可视化窗口 ────────────────────────────────────────────────────────────────────────────
function Show-PackagerGui {
    param([string]$RepoRoot)

    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $components = Get-ToolboxComponents -RepoRoot $RepoRoot
    $script:GuiActive = $true   # 外部命令等待期间要刷新窗口，不然界面会「未响应」

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "如意扩展组件打包器 —— ruyi-toolbox"
    $form.Size = New-Object System.Drawing.Size(780, 780)
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
        $gbHeight += 52   # 「依赖库」计划说明（勾上才显示，最多三行）
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
        $chkOffline.Text = "打包依赖库（完全离线安装；本机已有的直接打，缺的才下载；体积会明显变大）"
        $chkOffline.Checked = $false
        $chkOffline.Location = New-Object System.Drawing.Point(12, $my)
        $chkOffline.AutoSize = $true
        $gb.Controls.Add($chkOffline)
        $row.OfflineDepsChk = $chkOffline
        $my += 22
        # 事件处理里用 $sender.Tag 拿到这一组的上下文（不用闭包：循环里的 $c／$row 到事件触发时早就是最后一项了）。
        $ctx = @{ Comp = $c; Chk = $chkOffline; Combo = $null; KeyList = @(); PlanLabel = $null }
        if ($c.GpuTorch) {
            $lblGpu = New-Object System.Windows.Forms.Label
            $lblGpu.Text = "打包哪种显卡的 PyTorch："
            $lblGpu.Location = New-Object System.Drawing.Point(30, $my)
            $lblGpu.AutoSize = $true
            $gb.Controls.Add($lblGpu)
            $cbGpu = New-Object System.Windows.Forms.ComboBox
            $cbGpu.DropDownStyle = "DropDownList"
            $cbGpu.Location = New-Object System.Drawing.Point(230, ($my - 3))
            $cbGpu.Size = New-Object System.Drawing.Size(440, 22)
            $ctx.KeyList = @($c.GpuOrder)
            $ctx.Combo = $cbGpu
            $cbGpu.Tag = $ctx
            # 每一项标明"本机已装（直接用）"还是"需下载"；默认选本机已装的那一种，没有 venv 就选第一个。
            $localVariant = (Get-DepsPlan -Comp $c -GpuKey "").LocalTorch
            foreach ($k in $ctx.KeyList) {
                $g = $c.GpuTorch[$k]
                $tag = if ($k -eq $localVariant) { "  —— 本机已装，直接用" } else { "  —— 需下载（" + $g.Approx + "）" }
                [void]$cbGpu.Items.Add($g.Label + $tag)
            }
            $sel = [Array]::IndexOf($ctx.KeyList, $localVariant)
            if ($sel -lt 0) { $sel = 0 }
            $cbGpu.SelectedIndex = $sel
            $cbGpu.Enabled = $false
            $gb.Controls.Add($cbGpu)
            $row.GpuCombo = $cbGpu
            $my += 24
        }
        $lblPlan = New-Object System.Windows.Forms.Label
        $lblPlan.Location = New-Object System.Drawing.Point(30, $my)
        $lblPlan.Size = New-Object System.Drawing.Size(650, 48)
        $lblPlan.ForeColor = [System.Drawing.Color]::DimGray
        $lblPlan.Visible = $false
        $gb.Controls.Add($lblPlan)
        $ctx.PlanLabel = $lblPlan
        $chkOffline.Tag = $ctx
        $chkOffline.Add_CheckedChanged({ param($sender, $e) Update-DepsPlanUi -Ctx $sender.Tag })
        if ($ctx.Combo) { $ctx.Combo.Add_SelectedIndexChanged({ param($sender, $e) Update-DepsPlanUi -Ctx $sender.Tag }) }

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
    # 「取消」：打包过程中才可用。点了之后长循环（下载、还原、复制、压缩）会在下一个检查点停下，暂存文件自动清理。
    $btnCancel = New-Object System.Windows.Forms.Button
    $btnCancel.Text = "取消打包"
    $btnCancel.Location = New-Object System.Drawing.Point(252, $y2)
    $btnCancel.Size = New-Object System.Drawing.Size(110, 32)
    $btnCancel.Enabled = $false
    $form.Controls.Add($btnCancel)

    # 进度：一行「百分比 · 阶段 · 细节」＋ 进度条 ＋ 一行「已用／预计剩余」
    $y2 += 40
    $lblProg = New-Object System.Windows.Forms.Label
    $lblProg.Text = "就绪"
    $lblProg.Location = New-Object System.Drawing.Point(12, $y2)
    $lblProg.Size = New-Object System.Drawing.Size(742, 18)
    $lblProg.Anchor = "Left,Right,Bottom"
    $form.Controls.Add($lblProg)
    $progressBar = New-Object System.Windows.Forms.ProgressBar
    $progressBar.Minimum = 0
    $progressBar.Maximum = 1000
    $progressBar.Location = New-Object System.Drawing.Point(12, ($y2 + 20))
    $progressBar.Size = New-Object System.Drawing.Size(742, 18)
    $progressBar.Anchor = "Left,Right,Bottom"
    $form.Controls.Add($progressBar)
    $lblTime = New-Object System.Windows.Forms.Label
    $lblTime.Text = ""
    $lblTime.ForeColor = [System.Drawing.Color]::DimGray
    $lblTime.Location = New-Object System.Drawing.Point(12, ($y2 + 42))
    $lblTime.Size = New-Object System.Drawing.Size(742, 18)
    $lblTime.Anchor = "Left,Right,Bottom"
    $form.Controls.Add($lblTime)

    $y2 += 66
    $logBox = New-Object System.Windows.Forms.TextBox
    $logBox.Multiline = $true
    $logBox.ReadOnly = $true
    $logBox.ScrollBars = "Vertical"
    $logBox.Font = New-Object System.Drawing.Font("Consolas", 9)
    $logBox.Location = New-Object System.Drawing.Point(12, $y2)
    $logBox.Size = New-Object System.Drawing.Size(742, 150)
    $logBox.Anchor = "Left,Right,Bottom"
    $form.Controls.Add($logBox)

    $script:Packing = $false
    $btnCancel.Add_Click({
        $script:CancelRequested = $true
        $btnCancel.Enabled = $false
        $lblProg.Text = "正在取消……（停在下一个检查点，暂存文件会自动清理）"
    })
    # 打包进行中点了窗口右上角的 ×：不能直接关（后台还在写好几个 GB 的文件），先取消，等它收拾干净后再关。
    $form.Add_FormClosing({
        param($sender, $e)
        if ($script:Packing) {
            $e.Cancel = $true
            $script:CancelRequested = $true
            $btnCancel.Enabled = $false
            $lblProg.Text = "正在取消……收拾干净后再点一次关闭。"
        }
    })

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
                    $gpuKeys = $row.GpuCombo.Tag.KeyList
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
        foreach ($k in @($logBox, $lblProg, $progressBar, $lblTime, $btnCancel)) { $k.Enabled = $true }
        $logBox.Clear()
        $btnOpen.Enabled = $false
        $progressBar.Value = 0
        $lblProg.Text = "开始……"
        $lblTime.Text = ""
        $script:Packing = $true
        $script:CancelRequested = $false
        try {
            $logCb = { param($m) $logBox.AppendText($m + "`r`n"); $logBox.SelectionStart = $logBox.TextLength; $logBox.ScrollToCaret(); [System.Windows.Forms.Application]::DoEvents() }
            $repCb = {
                param($s)
                $progressBar.Value = [int][Math]::Min(1000, [Math]::Floor($s.Fraction * 1000))
                $lblProg.Text = ("{0}%   {1}   {2}" -f $s.Percent, $s.Label, $s.Detail)
                $eta = if ($s.EtaSec -ge 0) { "预计剩余 " + (Format-Duration $s.EtaSec) } elseif ($s.Fraction -ge 1.0) { "已完成" } else { "预计剩余：估算中……" }
                $lblTime.Text = ("已用 " + (Format-Duration $s.ElapsedSec) + "    " + $eta)
                [System.Windows.Forms.Application]::DoEvents()
            }
            $result = New-ToolboxBundle -RepoRoot $RepoRoot -ComponentIds $selectedIds -ModelSelection $modelSel -OfflineDeps $offlineDeps `
                -OutputDir $outBox.Text.Trim() -ZipName $nameBox.Text.Trim() `
                -IncludeTests:$chkTests.Checked -KeepStagingDir:$chkKeep.Checked -Log $logCb -Report $repCb
            $progressBar.Value = 1000
            $lblProg.Text = "100%   完成"
            & $logCb ("`n完成！" + (Format-Bytes $result.ZipBytes) + " —— " + $result.ZipPath)
            $lastZipPath = $result.ZipPath
            $btnOpen.Enabled = $true
        } catch {
            $msg = $_.Exception.Message
            if ($msg -match '已取消') {
                $lblProg.Text = "已取消"
                $logBox.AppendText("`r`n已取消。暂存文件已清理，没有留下半截的压缩包。`r`n")
            } else {
                $lblProg.Text = "打包失败"
                $logBox.AppendText("`r`n打包失败：" + $msg + "`r`n")
            }
        } finally {
            $script:Packing = $false
            $script:CancelRequested = $false
            $form.Controls | ForEach-Object { $_.Enabled = $true }
            $btnCancel.Enabled = $false
        }
    })

    [void]$form.ShowDialog()
    $script:GuiActive = $false
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
