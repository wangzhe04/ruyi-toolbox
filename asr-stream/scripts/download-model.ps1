#Requires -Version 5.1
<#
.SYNOPSIS
  把 sherpa-onnx 的流式 Zipformer 模型下到 asr-stream\models\ 下，然后向如意登记本组件。

.DESCRIPTION
  缺省从 GitHub releases 直连下一个 tar.bz2（Windows 自带 tar 能解）；大陆直连慢或不通时用 -Source hf
  走 HuggingFace 镜像逐文件下（尊重 HF_ENDPOINT，缺省 https://hf-mirror.com）。

  缺省模型：sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20（中英双语，int8 编码器约 174 MB）。
  只要 tokens.txt + encoder/decoder/joiner 三个 onnx；解压包里的 test_wavs 等顺手留着，不影响。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\download-model.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\download-model.ps1 -Source hf
#>
[CmdletBinding()]
param(
    [string]$Model = "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
    [ValidateSet("github", "hf")]
    [string]$Source = "github",
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

if ([string]::IsNullOrWhiteSpace($Dest)) {
    $Dest = Join-Path $root ("models\" + $Model)
}
$Dest = [System.IO.Path]::GetFullPath($Dest)
$modelsDir = Split-Path -Parent $Dest
New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null

Write-Host ("模型：" + $Model)
Write-Host ("来源：" + $Source)
Write-Host ("落地：" + $Dest)

if (Test-Path (Join-Path $Dest "tokens.txt")) {
    Write-Host "模型目录已经在了，跳过下载（想重下就先删掉它）。"
}
elseif ($Source -eq "github") {
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($null -eq $tar) { throw "没有 tar.exe（Windows 10 1803 起自带）。改用 -Source hf。" }
    $url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/" + $Model + ".tar.bz2"
    $archive = Join-Path $modelsDir ($Model + ".tar.bz2")
    Write-Host ("下载：" + $url)
    Write-Host "（约 200 MB；大陆直连 GitHub 可能慢，慢就 Ctrl+C 改用 -Source hf）"
    & curl.exe -L --fail --retry 3 --retry-delay 2 -o $archive $url
    if ($LASTEXITCODE -ne 0) { throw "下载失败。改用 -Source hf 再试。" }
    Write-Host "解压…"
    & $tar.Source -xjf $archive -C $modelsDir
    if ($LASTEXITCODE -ne 0) { throw "解压失败（文件可能没下全）。删掉 " + $archive + " 重来。" }
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
}
else {
    $endpoint = $env:HF_ENDPOINT
    if ([string]::IsNullOrWhiteSpace($endpoint)) { $endpoint = "https://hf-mirror.com" }
    $endpoint = $endpoint.TrimEnd("/")
    Write-Host ("HF_ENDPOINT = " + $endpoint)
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    # 只拿跑起来要用的四个文件（int8 编码器与 joiner；decoder 用 fp32）。
    $files = @("tokens.txt", "encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx", "joiner-epoch-99-avg-1.int8.onnx")
    foreach ($f in $files) {
        $url = $endpoint + "/csukuangfj/" + $Model + "/resolve/main/" + $f
        Write-Host ("下载：" + $f)
        & curl.exe -L --fail --retry 3 --retry-delay 2 -o (Join-Path $Dest $f) $url
        if ($LASTEXITCODE -ne 0) { throw ("下载失败：" + $f + "（换个 HF_ENDPOINT 或改用 -Source github）") }
    }
}

# 解压出来的目录名就是模型名；万一 tar 里套了别的层级，找 tokens.txt 定位。
if (-not (Test-Path (Join-Path $Dest "tokens.txt"))) {
    $found = Get-ChildItem -Path $modelsDir -Recurse -Filter tokens.txt -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like ("*" + $Model + "*") } | Select-Object -First 1
    if ($null -eq $found) { throw ("下完了，但 " + $Dest + " 里没有 tokens.txt —— 看着没下全。") }
    $Dest = $found.DirectoryName
}
Write-Host ""
Write-Host ("模型就位：" + $Dest) -ForegroundColor Green

if ($NoRegister) {
    Write-Host "-NoRegister：跳过登记。要让如意自动接入，手动跑："
    Write-Host ("  " + $py + " -m ruyi_asr_stream register --model-dir """ + $Dest + """")
    exit 0
}

Write-Host ""
Write-Host "==> 向如意登记本组件" -ForegroundColor Cyan
& $py -m ruyi_asr_stream register --model-dir $Dest
if ($LASTEXITCODE -ne 0) {
    Write-Host "登记没成（见上面的原因）。服务本身仍然可以手动起：scripts\start.ps1" -ForegroundColor Yellow
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "好了。如意下次启动就会自动发现并拉起它，输入框的麦克风会变成边说边出字。" -ForegroundColor Green
exit 0
