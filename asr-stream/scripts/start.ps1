#Requires -Version 5.1
<#
.SYNOPSIS
  前台起 asr-stream（排障用；如意自动接入之后不需要跑这个）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Port 8792 -Rule2Sec 1.0
#>
[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$ModelDir = "",
    [double]$Rule2Sec = 0,
    [string]$HotwordsFile = ""
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "还没建虚拟环境。先跑 scripts\install.ps1。" -ForegroundColor Red
    exit 1
}

if ($Port -gt 0) { $env:RUYI_ASR_STREAM_PORT = [string]$Port }
if (-not [string]::IsNullOrWhiteSpace($ModelDir)) { $env:RUYI_ASR_STREAM_MODEL_DIR = $ModelDir }
if ($Rule2Sec -gt 0) { $env:RUYI_ASR_STREAM_RULE2_SEC = [string]$Rule2Sec }
if (-not [string]::IsNullOrWhiteSpace($HotwordsFile)) { $env:RUYI_ASR_STREAM_HOTWORDS_FILE = $HotwordsFile }

if ([string]::IsNullOrWhiteSpace($env:RUYI_ASR_STREAM_MODEL_DIR)) {
    $guess = Get-ChildItem -Path (Join-Path $root "models") -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName "tokens.txt") } | Select-Object -First 1
    if ($null -ne $guess) {
        $env:RUYI_ASR_STREAM_MODEL_DIR = $guess.FullName
        Write-Host ("模型目录：" + $guess.FullName)
    }
    else {
        Write-Host "没找到模型目录。先跑 scripts\download-model.ps1。" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Ctrl+C 停止。"
Write-Host ""
Set-Location $root
& $py -m ruyi_asr_stream
exit $LASTEXITCODE
