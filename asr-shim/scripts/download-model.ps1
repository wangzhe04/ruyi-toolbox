#Requires -Version 5.1
<#
.SYNOPSIS
  把 Qwen3-ASR 模型下到 asr-shim\models\ 下，然后向如意登记本组件。

.DESCRIPTION
  缺省走 ModelScope（魔搭，大陆直连快）；-Source hf 走 HuggingFace，并尊重已设的
  HF_ENDPOINT 镜像（例如 https://hf-mirror.com）。

  注意下的是【带 -hf 后缀】的那一份仓库（Qwen/Qwen3-ASR-0.6B-hf）：原生 transformers
  只认它；不带 -hf 的那份 config 是 thinker 嵌套结构，是给官方 qwen-asr 包用的。
  依据见 docs\backend-notes.md。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\download-model.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\download-model.ps1 -Model qwen3-asr-1.7b -Source hf
#>
[CmdletBinding()]
param(
    [ValidateSet("qwen3-asr-0.6b", "qwen3-asr-1.7b")]
    [string]$Model = "qwen3-asr-0.6b",
    [ValidateSet("modelscope", "hf")]
    [string]$Source = "modelscope",
    [string]$Dest = "",
    [switch]$NoRegister
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "还没建虚拟环境。先跑：" -ForegroundColor Red
    Write-Host ("  powershell -ExecutionPolicy Bypass -File " + (Join-Path $here "install.ps1"))
    exit 1
}

$repoMap = @{
    "qwen3-asr-0.6b" = "Qwen/Qwen3-ASR-0.6B-hf"
    "qwen3-asr-1.7b" = "Qwen/Qwen3-ASR-1.7B-hf"
}
$repo = $repoMap[$Model]
$leaf = $repo.Split("/")[-1]

if ([string]::IsNullOrWhiteSpace($Dest)) {
    $Dest = Join-Path $root ("models\" + $leaf)
}
$Dest = [System.IO.Path]::GetFullPath($Dest)

Write-Host ("模型：" + $repo)
Write-Host ("来源：" + $Source)
Write-Host ("落地：" + $Dest)
Write-Host "0.6B 约 1.5 GB，1.7B 更大；第一次会下一会儿。"
Write-Host ""

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null

if ($Source -eq "modelscope") {
    & $py -c "import modelscope" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "没装 modelscope。装一下：" -ForegroundColor Red
        Write-Host ("  uv pip install --python " + $py + " modelscope")
        Write-Host "或者改用 -Source hf。"
        exit 1
    }
    & $py -m modelscope.cli.cli download --model $repo --local_dir $Dest
    if ($LASTEXITCODE -ne 0) {
        Write-Host "从魔搭下载失败。可以改用 -Source hf 再试。" -ForegroundColor Red
        exit 1
    }
}
else {
    if ([string]::IsNullOrWhiteSpace($env:HF_ENDPOINT)) {
        Write-Host "提示：大陆直连 huggingface.co 常常不通。可以先设镜像再跑本脚本：" -ForegroundColor Yellow
        Write-Host '  $env:HF_ENDPOINT = "https://hf-mirror.com"'
    }
    else {
        Write-Host ("HF_ENDPOINT = " + $env:HF_ENDPOINT)
    }
    $code = @"
import sys
from huggingface_hub import snapshot_download
p = snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2])
print(p)
"@
    & $py -c $code $repo $Dest
    if ($LASTEXITCODE -ne 0) {
        Write-Host "从 HuggingFace 下载失败。设个镜像（HF_ENDPOINT）或改用 -Source modelscope。" -ForegroundColor Red
        exit 1
    }
}

$cfg = Join-Path $Dest "config.json"
if (-not (Test-Path $cfg)) {
    Write-Host ("下完了，但 " + $cfg + " 不在 —— 看着没下全。") -ForegroundColor Red
    exit 1
}
Write-Host ""
Write-Host ("模型就位：" + $Dest) -ForegroundColor Green

$modelsRoot = Split-Path -Parent $Dest
if ($NoRegister) {
    Write-Host "-NoRegister：跳过登记。要让如意自动接入，手动跑："
    Write-Host ("  " + $py + " -m ruyi_asr_shim register --model auto --models-root """ + $modelsRoot + """")
    exit 0
}

# 登记成 auto：models 目录里有几份（0.6B / 1.7B）就在第一发请求时按空闲显存挑最大能装下的；
# 以后再下一份更大的，不用重新登记。想钉死某一份：register --model-dir <目录> --model <名>。
Write-Host ""
Write-Host "==> 向如意登记本组件（auto：按显存挑最大能装下的模型）" -ForegroundColor Cyan
& $py -m ruyi_asr_shim register --model auto --models-root $modelsRoot
if ($LASTEXITCODE -ne 0) {
    Write-Host "登记没成（见上面的原因）。服务本身仍然可以手动起：scripts\start.ps1" -ForegroundColor Yellow
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "好了。如意下次启动就会自动发现并拉起它。" -ForegroundColor Green
Write-Host "想自己先起来看看：powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1"
exit 0
