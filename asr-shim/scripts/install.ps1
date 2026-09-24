#Requires -Version 5.1
<#
.SYNOPSIS
  给 asr-shim 建 venv、装对的 PyTorch（按显卡厂商挑构建）与其余依赖，最后自检一遍。

.DESCRIPTION
  为什么 torch 不写在 pyproject.toml 里：同一个包名，英伟达要 cu128 构建、AMD 要 ROCm 构建、
  没有独显就要 CPU 构建，PyPI 的默认轮子只对第一种情况勉强适用。所以 torch 由本脚本按
  -Gpu 挑源装，其余依赖走常规 PyPI。

  -Gpu auto   看 Win32_VideoController 认厂商（缺省）
  -Gpu nvidia 装 cu128 构建（Blackwell/RTX 50 系必须 cu128 及以上）
  -Gpu amd    装 AMD 官方 ROCm on Windows 的轮子（要求 Python 3.12 + 26.2.2 及以上的显卡驱动，
              且显卡在 AMD 的支持列表里：RX 9070/9070XT/9060XT、RX 7900XTX/7700、W7900、
              AI PRO R9700，或 Ryzen AI Max+ 395 这类 gfx1150/1151 APU）
              已在 RX 7650 GRE（gfx1102，不在官方列表里）上真机跑通；其它卡没测过。
  -Gpu cpu    装 CPU 构建（能跑，但很慢）

  跑完之后下一步是 scripts\download-model.ps1（拉模型并向如意登记本组件）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Gpu amd
  powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Recreate
#>
[CmdletBinding()]
param(
    [ValidateSet("auto", "nvidia", "amd", "cpu")]
    [string]$Gpu = "auto",
    [string]$PythonVersion = "3.12",
    [switch]$Recreate,
    [string]$CudaIndex = "https://download.pytorch.org/whl/cu128",
    [string]$CpuIndex = "https://download.pytorch.org/whl/cpu",
    [string]$RocmRelease = "7.2.1"
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"

# 进度：本脚本共几步、现在第几步。每一步的标题前带上 [第几步/共几步]，并用 Write-Progress 画进度条
# （被 setup.ps1 调用时，这条进度条挂在它的「安装扩展组件」总进度条下面）。总步数在下面确定了要不要建 venv 等之后再算准。
$script:StepIndex = 0
$script:StepTotal = 1

function Write-StepHeader {
    param([string]$Title)
    $script:StepIndex++
    Write-Host ""
    Write-Host ("==> [" + $script:StepIndex + "/" + $script:StepTotal + "] " + $Title) -ForegroundColor Cyan
    Write-Progress -Id 2 -ParentId 1 -Activity "安装 asr-shim" `
        -Status ("[" + $script:StepIndex + "/" + $script:StepTotal + "] " + $Title) `
        -PercentComplete ([int](100 * ($script:StepIndex - 1) / [Math]::Max($script:StepTotal, 1)))
}

function Invoke-Step {
    param([string]$Title, [scriptblock]$Body)
    Write-StepHeader $Title
    & $Body
    if ($LASTEXITCODE -ne 0) {
        throw ("这一步失败了（退出码 " + $LASTEXITCODE + "）：" + $Title)
    }
}

function Get-GpuVendor {
    # 认厂商只看显卡名。认不出来就回 cpu —— 宁可装个能跑的，也不要装个起不来的。
    try {
        $names = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop | ForEach-Object { $_.Name })
    }
    catch {
        Write-Host "问不到显卡信息，按 CPU 处理。" -ForegroundColor Yellow
        return "cpu"
    }
    foreach ($n in $names) { Write-Host ("  显卡：" + $n) }
    foreach ($n in $names) {
        if ($n -match "NVIDIA|GeForce|Quadro|Tesla|RTX") { return "nvidia" }
    }
    foreach ($n in $names) {
        if ($n -match "AMD|Radeon|ATI") { return "amd" }
    }
    return "cpu"
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    Write-Host "没找到 uv。先装它：" -ForegroundColor Red
    Write-Host '  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Write-Host "或者 pip install uv。装完重开一个 PowerShell 再跑本脚本。"
    exit 1
}

Write-Host ("asr-shim 安装目录：" + $root)
Write-Host ("uv：" + $uv.Source)

if ($Gpu -eq "auto") {
    Write-Host ""
    Write-Host "==> 认一下显卡" -ForegroundColor Cyan
    $Gpu = Get-GpuVendor
    Write-Host ("  判定：" + $Gpu)
}
else {
    Write-Host ("显卡（你指定的）：" + $Gpu)
}

if ($Recreate -and (Test-Path $venv)) {
    Write-Host "-Recreate：删掉旧的 .venv 重来"
    Remove-Item -Recurse -Force $venv
}

# 总步数：建 venv（已有就不算）＋ 装 PyTorch（AMD 是 SDK＋torch 两步，其余一步）＋ 装本体与依赖 ＋ 装 modelscope ＋ 自检。
$script:StepTotal = $(if (Test-Path $py) { 0 } else { 1 }) + $(if ($Gpu -eq "amd") { 2 } else { 1 }) + 3

if (-not (Test-Path $py)) {
    Invoke-Step ("建虚拟环境（Python " + $PythonVersion + "）") {
        & uv venv --python $PythonVersion $venv
    }
}
else {
    Write-Host "已有 .venv，直接用它（要从零重来加 -Recreate）"
}

if ($Gpu -eq "nvidia") {
    # cu128 是硬要求：老的 cu121/cu124 轮子在 Blackwell(sm_120) 上跑不起来。约 3 GB。
    Invoke-Step "装 PyTorch（cu128 构建，约 3 GB，第一次会很久）" {
        & uv pip install --python $py --index-url $CudaIndex torch
    }
}
elseif ($Gpu -eq "amd") {
    # 依据：AMD 官方「Install PyTorch via PIP (ROCm on Radeon, Windows)」，见 docs\backend-notes.md §8。
    # 轮子只有 cp312 —— 所以上面那个 venv 必须是 Python 3.12。
    if ($PythonVersion -ne "3.12") {
        Write-Host "AMD 的 ROCm on Windows 轮子只有 cp312（Python 3.12）。" -ForegroundColor Red
        Write-Host "请不要改 -PythonVersion，或者改用 -Gpu cpu。"
        exit 1
    }
    Write-Host ""
    Write-Host "注意：这条 AMD 路线只在 RX 7650 GRE（gfx1102）上真机验证过，其它卡没测过。" -ForegroundColor Yellow
    Write-Host "前提：显卡驱动 26.2.2 及以上，且显卡在 AMD 的 Windows 支持列表里。" -ForegroundColor Yellow
    Write-Host "官方页面（装不上时以它为准）：" -ForegroundColor Yellow
    Write-Host "  https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installrad/windows/install-pytorch.html"

    # 这几个是直链而不是 index，uv 的 --offline／--find-links 管不到它们。用 tools\package-bundle.ps1 打了
    # 「依赖库」的 AMD 包会把它们放在 .offline-wheels\rocm\ 里——有就直接装本地文件，不联网。
    $rocmLocal = Join-Path $root ".offline-wheels\rocm"
    if (Test-Path -LiteralPath $rocmLocal) {
        Write-Host ("用 " + $rocmLocal + " 里带着的 ROCm 文件，不联网。")
        $sdkFiles = @(Get-ChildItem -LiteralPath $rocmLocal -File | Where-Object { $_.Name -match '^(rocm_sdk_.*\.whl|rocm-.*\.tar\.gz)$' } | ForEach-Object { $_.FullName })
        $torchFiles = @(Get-ChildItem -LiteralPath $rocmLocal -File -Filter "torch-*.whl" | ForEach-Object { $_.FullName })
        if ($sdkFiles.Count -eq 0 -or $torchFiles.Count -eq 0) {
            Write-Host ("这个目录里的 ROCm 文件不全（SDK " + $sdkFiles.Count + " 个、torch " + $torchFiles.Count + " 个）。") -ForegroundColor Red
            exit 1
        }
    }
    else {
        $base = "https://repo.radeon.com/rocm/windows/rocm-rel-" + $RocmRelease + "/"
        $sdkFiles = @(
            ($base + "rocm_sdk_core-" + $RocmRelease + "-py3-none-win_amd64.whl"),
            ($base + "rocm_sdk_devel-" + $RocmRelease + "-py3-none-win_amd64.whl"),
            ($base + "rocm_sdk_libraries_custom-" + $RocmRelease + "-py3-none-win_amd64.whl"),
            ($base + "rocm-" + $RocmRelease + ".tar.gz"))
        $torchFiles = @($base + "torch-2.9.1%2Brocm" + $RocmRelease + "-cp312-cp312-win_amd64.whl")
    }
    Invoke-Step ("装 ROCm SDK " + $RocmRelease) {
        & uv pip install --python $py --no-cache @sdkFiles
    }
    Invoke-Step ("装 PyTorch（ROCm " + $RocmRelease + " 构建）") {
        & uv pip install --python $py --no-cache @torchFiles
    }
}
else {
    Invoke-Step "装 PyTorch（CPU 构建）" {
        & uv pip install --python $py --index-url $CpuIndex torch
    }
    Write-Host "装的是 CPU 构建，推理会慢。有独显的话用 -Gpu nvidia 或 -Gpu amd。" -ForegroundColor Yellow
}

Invoke-Step "装 asr-shim 自己与其余依赖（transformers / accelerate / soundfile / numpy）" {
    & uv pip install --python $py -e $root
}

# 下模型要用；放在这里省得用户再折腾一次。装不上也不致命（还能走 HuggingFace 那条路）。
Write-StepHeader "装 modelscope（从魔搭下模型用，大陆推荐）"
& uv pip install --python $py "modelscope>=1.20"
if ($LASTEXITCODE -ne 0) {
    Write-Host "modelscope 没装上。不致命 —— 下模型时可以用 -Source hf 走 HuggingFace。" -ForegroundColor Yellow
}

Write-StepHeader "自检"
& $py -m ruyi_asr_shim doctor
$doctorCode = $LASTEXITCODE

Write-Progress -Id 2 -Activity "安装 asr-shim" -Completed
Write-Host ""
if ($doctorCode -eq 0) {
    Write-Host "装好了。" -ForegroundColor Green
}
else {
    Write-Host "装完了，但自检有问题（见上面几行）。没有显卡加速时服务照样能跑，只是会慢。" -ForegroundColor Yellow
}
Write-Host "下一步："
Write-Host ("  powershell -ExecutionPolicy Bypass -File " + (Join-Path $here "download-model.ps1"))
Write-Host "它会把 Qwen3-ASR-0.6B 拉下来（约 1.5 GB）并向如意登记本组件。"
exit 0
