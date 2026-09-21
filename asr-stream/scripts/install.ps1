#Requires -Version 5.1
<#
.SYNOPSIS
  给 asr-stream 建 venv、装 sherpa-onnx（CPU 轮子，不用 torch、不用显卡），最后自检。

.DESCRIPTION
  流式小模型跑在 CPU 上，PyPI 的 sherpa-onnx 有 cp312 win_amd64 轮子，整个环境不到 50 MB。
  跑完之后下一步是 scripts\download-model.ps1（拉模型并向如意登记本组件）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Recreate
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12",
    [switch]$Recreate
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
    Write-Progress -Id 2 -ParentId 1 -Activity "安装 asr-stream" `
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

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    Write-Host "没找到 uv。先装它：" -ForegroundColor Red
    Write-Host '  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Write-Host "或者 pip install uv。装完重开一个 PowerShell 再跑本脚本。"
    exit 1
}

Write-Host ("asr-stream 安装目录：" + $root)
Write-Host ("uv：" + $uv.Source)

if ($Recreate -and (Test-Path $venv)) {
    Write-Host "-Recreate：删掉旧的 .venv 重来"
    Remove-Item -Recurse -Force $venv
}

# 总步数：建 venv（已有就不算）＋ 装本体与依赖 ＋ 自检。
$script:StepTotal = $(if (Test-Path $py) { 0 } else { 1 }) + 2

if (-not (Test-Path $py)) {
    Invoke-Step ("建虚拟环境（Python " + $PythonVersion + "）") {
        & uv venv --python $PythonVersion $venv
    }
}
else {
    Write-Host "已有 .venv，直接用它（要从零重来加 -Recreate）"
}

Invoke-Step "装 asr-stream 自己与依赖（sherpa-onnx / numpy）" {
    & uv pip install --python $py -e $root
}

Write-StepHeader "自检"
& $py -m ruyi_asr_stream doctor
$doctorCode = $LASTEXITCODE

Write-Progress -Id 2 -Activity "安装 asr-stream" -Completed
Write-Host ""
if ($doctorCode -eq 0) {
    Write-Host "装好了。" -ForegroundColor Green
}
else {
    Write-Host "装完了，但自检有问题（多半是还没下模型，见上面几行）。" -ForegroundColor Yellow
}
Write-Host "下一步："
Write-Host ("  powershell -ExecutionPolicy Bypass -File " + (Join-Path $here "download-model.ps1"))
Write-Host "它会把流式模型拉下来（约 200 MB）并向如意登记本组件。"
exit 0
