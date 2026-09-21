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

function Invoke-Step {
    param([string]$Title, [scriptblock]$Body)
    Write-Host ""
    Write-Host ("==> " + $Title) -ForegroundColor Cyan
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

Write-Host ""
Write-Host "==> 自检" -ForegroundColor Cyan
& $py -m ruyi_asr_stream doctor
$doctorCode = $LASTEXITCODE

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
