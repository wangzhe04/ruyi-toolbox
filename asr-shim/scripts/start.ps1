#Requires -Version 5.1
<#
.SYNOPSIS
  前台起 asr-shim，并打印如意里要填的地址与模型名。

.DESCRIPTION
  如意自动接入之后【不需要】跑这个脚本 —— 它会自己拉起。这个脚本给两种情况用：
  排障时想看日志；或者你不想让如意管它，自己起一个（如意探到端口上已经是本组件就不会再拉一个）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Port 8791 -IdleUnloadSec 60
#>
[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$ModelDir = "",
    [string]$Model = "",
    [int]$IdleUnloadSec = -1,
    [ValidateSet("", "auto", "cuda", "directml", "cpu")]
    [string]$Device = ""
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "还没建虚拟环境。先跑 scripts\install.ps1。" -ForegroundColor Red
    exit 1
}

if ($Port -gt 0) { $env:RUYI_ASR_PORT = [string]$Port }
if (-not [string]::IsNullOrWhiteSpace($ModelDir)) { $env:RUYI_ASR_MODEL_DIR = $ModelDir }
if (-not [string]::IsNullOrWhiteSpace($Model)) { $env:RUYI_ASR_MODEL = $Model }
if ($IdleUnloadSec -ge 0) { $env:RUYI_ASR_IDLE_UNLOAD_SEC = [string]$IdleUnloadSec }
if (-not [string]::IsNullOrWhiteSpace($Device)) { $env:RUYI_ASR_DEVICE = $Device }

# 没显式给模型目录就按 download-model.ps1 的缺省落点找一个，省得用户每次都填。
if ([string]::IsNullOrWhiteSpace($env:RUYI_ASR_MODEL_DIR)) {
    $guess = Join-Path $root "models\Qwen3-ASR-0.6B-hf"
    if (Test-Path $guess) {
        $env:RUYI_ASR_MODEL_DIR = $guess
        Write-Host ("模型目录：" + $guess)
    }
    else {
        Write-Host "没找到本地模型目录，服务会尝试联网下载（大陆可能很慢）。" -ForegroundColor Yellow
        Write-Host "建议先跑 scripts\download-model.ps1。"
    }
}

Write-Host "Ctrl+C 停止。"
Write-Host ""
Set-Location $root
& $py -m ruyi_asr_shim
exit $LASTEXITCODE
