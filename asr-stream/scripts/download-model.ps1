#Requires -Version 5.1
<#
.SYNOPSIS
  把 sherpa-onnx 的流式 Zipformer 模型下到 asr-stream\models\ 下，然后向如意登记本组件。

.DESCRIPTION
  缺省从 GitHub releases 直连下一个 tar.bz2（Windows 自带 tar 能解）；大陆直连慢或不通时用 -Source hf
  走 HuggingFace 镜像逐文件下（尊重 HF_ENDPOINT，缺省 https://hf-mirror.com）。

  缺省模型：sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20（中英双语，int8 编码器约 174 MB）。
  只要 tokens.txt + encoder/decoder/joiner 三个 onnx，外加 bpe.vocab（英文热词切词用，缺了只影响英文热词）；
  解压包里的 test_wavs 等顺手留着，不影响。

  「已经下好了」的判据与 register 自检同一口径（必需文件个个在、非零字节）；所有下载先写 <文件>.part、
  完整了才改名，压缩包先解到临时目录、解完整才挪成正式目录。上次下载／解压中断留下的半截目录，重跑会删掉重下。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\download-model.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\download-model.ps1 -Source hf
#>
[CmdletBinding()]
param(
    # 模型名会拼成 models 下的目录名（半截目录会被整个删掉重下），所以只许字母数字开头的普通名字
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$Model = "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
    [ValidateSet("github", "hf")]
    [string]$Source = "github",
    [string]$Dest = "",
    [switch]$NoRegister,
    # 131c：一并下载 SenseVoice（离线整句识别，句尾改错不靠显卡，约 230 MB）。不想要就 -NoOffline。
    [switch]$NoOffline,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$OfflineModel = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
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

# 「下全了没有」与 register 自检同一口径（ruyi_asr_stream.registry 的 stream_model_problems / offline_model_problems）：
# 必需文件个个都在、非零字节。只认 tokens.txt 的话，下载或解压中断留下的半截目录会被当成「已下载」永远跳过。
$checkCode = @'
import sys
from ruyi_asr_stream.registry import offline_model_problems, stream_model_problems
check = offline_model_problems if sys.argv[1] == 'offline' else stream_model_problems
problems = check(sys.argv[2])
for p in problems:
    print('  - ' + p)
sys.exit(1 if problems else 0)
'@

function Test-ModelReady([string]$Kind, [string]$Dir) {
    if (-not (Test-Path -LiteralPath $Dir)) { return $false }
    & $py -c $checkCode $Kind $Dir | Out-Host
    return ($LASTEXITCODE -eq 0)
}

# 从 GitHub releases 下 <Name>.tar.bz2 并解成 <IntoDir>\<Name>。压缩包先写 .part、下完整了才改名；
# 先解到同级的临时目录、整份解完才挪成正式目录 —— 下载或解压中途被打断，都不会留下「看着在、其实半截」的模型目录。
function Get-ModelArchive([string]$Name, [string]$IntoDir) {
    $url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/" + $Name + ".tar.bz2"
    $archive = Join-Path $IntoDir ($Name + ".tar.bz2")
    $part = $archive + ".part"
    Write-Host ("下载：" + $url)
    & curl.exe -L --fail --retry 3 --retry-delay 2 -o $part $url | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
        Write-Host "下载失败。" -ForegroundColor Yellow
        return $false
    }
    Move-Item -LiteralPath $part -Destination $archive -Force
    $stage = Join-Path $IntoDir (".extract-" + $Name)
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Write-Host "解压…"
    & tar.exe -xjf $archive -C $stage | Out-Host
    $ok = ($LASTEXITCODE -eq 0)
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    if (-not $ok) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "解压失败（压缩包可能坏了）。" -ForegroundColor Yellow
        return $false
    }
    $inner = Join-Path $stage $Name
    if (-not (Test-Path -LiteralPath $inner)) { $inner = $stage }   # 包里没套一层同名目录：整个临时目录就是它
    $target = Join-Path $IntoDir $Name
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Move-Item -LiteralPath $inner -Destination $target
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    return $true
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

if (Test-ModelReady "stream" $Dest) {
    Write-Host "模型已经下全了，跳过下载（想重下就先删掉它）。"
}
elseif ($Source -eq "github") {
    if (Test-Path -LiteralPath $Dest) { Write-Host "模型目录在，但没下全（见上面），删掉重下。" -ForegroundColor Yellow }
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($null -eq $tar) { throw "没有 tar.exe（Windows 10 1803 起自带）。改用 -Source hf。" }
    Write-Host "（约 200 MB；大陆直连 GitHub 可能慢，慢就 Ctrl+C 改用 -Source hf）"
    if (-not (Get-ModelArchive $Model $modelsDir)) { throw "下载或解压失败。改用 -Source hf 再试。" }
}
else {
    if (Test-Path -LiteralPath $Dest) { Write-Host "模型目录在，但没下全（见上面），重下必需的文件。" -ForegroundColor Yellow }
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
        # 先写 .part、下完整了才改名：中途断网不会留下顶着正式文件名的半截文件
        $part = Join-Path $Dest ($f + ".part")
        & curl.exe -L --fail --retry 3 --retry-delay 2 -o $part $url
        if ($LASTEXITCODE -ne 0) {
            Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
            throw ("下载失败：" + $f + "（换个 HF_ENDPOINT 或改用 -Source github）")
        }
        Move-Item -LiteralPath $part -Destination (Join-Path $Dest $f) -Force
    }
}

# 解压出来的目录名就是模型名；万一 tar 里套了别的层级，找 tokens.txt 定位。
if (-not (Test-ModelReady "stream" $Dest)) {
    $found = Get-ChildItem -Path $modelsDir -Recurse -Filter tokens.txt -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like ("*" + $Model + "*") } | Select-Object -First 1
    if (($null -eq $found) -or -not (Test-ModelReady "stream" $found.DirectoryName)) {
        throw ("下完了，但 " + $Dest + " 里的模型文件不全 —— 看着没下全。再跑一次本脚本会删掉半截的重下。")
    }
    $Dest = $found.DirectoryName
}
Write-Host ""
Write-Host ("模型就位：" + $Dest) -ForegroundColor Green

# 热词要按模型自己的 BPE 切英文（engine.detect_modeling_unit）：缺了 bpe.vocab，中文热词照常、英文热词切不出 token。
# GitHub 包里本来就有；HF 镜像那条路早先只下四个文件，老装好的目录也在这里补一次。只有几十 KB，下不到也不拦。
$vocab = Join-Path $Dest "bpe.vocab"
if (-not (Test-Path -LiteralPath $vocab) -or ((Get-Item -LiteralPath $vocab).Length -eq 0)) {
    $hfBase = $env:HF_ENDPOINT
    if ([string]::IsNullOrWhiteSpace($hfBase)) { $hfBase = "https://hf-mirror.com" }
    $vocabUrl = $hfBase.TrimEnd("/") + "/csukuangfj/" + $Model + "/resolve/main/bpe.vocab"
    Write-Host "补下 bpe.vocab（英文热词用）…"
    & curl.exe -L --fail --retry 2 --retry-delay 2 -o ($vocab + ".part") $vocabUrl
    if ($LASTEXITCODE -eq 0) {
        Move-Item -LiteralPath ($vocab + ".part") -Destination $vocab -Force
    }
    else {
        Remove-Item -LiteralPath ($vocab + ".part") -Force -ErrorAction SilentlyContinue
        Write-Host "bpe.vocab 没下到：中文热词照常，英文热词不生效（换个 HF_ENDPOINT 再跑一次本脚本即可补上）。" -ForegroundColor Yellow
    }
}

# ── 131c：离线整句识别（SenseVoice）——同一进程多一条 /v1/audio/transcriptions，如意里多一个不要显卡的「整段识别」候选
$offlineDir = ""
if (-not $NoOffline) {
    $offlineDir = Join-Path $modelsDir $OfflineModel
    Write-Host ""
    Write-Host ("离线整句识别模型：" + $OfflineModel)
    if (Test-ModelReady "offline" $offlineDir) {
        Write-Host "已经下全了，跳过下载。"
    }
    elseif ($Source -eq "github") {
        if (-not (Get-ModelArchive $OfflineModel $modelsDir)) {
            Write-Host "SenseVoice 下载或解压失败，跳过离线整句识别（可稍后 -Source hf 重跑）。" -ForegroundColor Yellow
            $offlineDir = ""
        }
    }
    else {
        $endpoint = $env:HF_ENDPOINT
        if ([string]::IsNullOrWhiteSpace($endpoint)) { $endpoint = "https://hf-mirror.com" }
        $endpoint = $endpoint.TrimEnd("/")
        New-Item -ItemType Directory -Force -Path $offlineDir | Out-Null
        foreach ($f in @("tokens.txt", "model.int8.onnx")) {
            $url = $endpoint + "/csukuangfj/" + $OfflineModel + "/resolve/main/" + $f
            Write-Host ("下载：" + $f)
            $part = Join-Path $offlineDir ($f + ".part")
            & curl.exe -L --fail --retry 3 --retry-delay 2 -o $part $url
            if ($LASTEXITCODE -ne 0) {
                Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
                Write-Host ("SenseVoice 下载失败：" + $f + "，跳过离线整句识别。") -ForegroundColor Yellow; $offlineDir = ""; break
            }
            Move-Item -LiteralPath $part -Destination (Join-Path $offlineDir $f) -Force
        }
    }
    if ($offlineDir -and -not (Test-ModelReady "offline" $offlineDir)) {
        Write-Host "SenseVoice 没下全，跳过离线整句识别（再跑一次本脚本会重下）。" -ForegroundColor Yellow
        $offlineDir = ""
    }
    if ($offlineDir) { Write-Host ("离线模型就位：" + $offlineDir) -ForegroundColor Green }
}

if ($NoRegister) {
    Write-Host "-NoRegister：跳过登记。要让如意自动接入，手动跑："
    if ($offlineDir) { Write-Host ("  " + $py + " -m ruyi_asr_stream register --model-dir """ + $Dest + """ --offline-model-dir """ + $offlineDir + """") }
    else { Write-Host ("  " + $py + " -m ruyi_asr_stream register --model-dir """ + $Dest + """") }
    exit 0
}

Write-Host ""
Write-Host "==> 向如意登记本组件" -ForegroundColor Cyan
if ($offlineDir) { & $py -m ruyi_asr_stream register --model-dir $Dest --offline-model-dir $offlineDir }
else { & $py -m ruyi_asr_stream register --model-dir $Dest }
if ($LASTEXITCODE -ne 0) {
    Write-Host "登记没成（见上面的原因）。服务本身仍然可以手动起：scripts\start.ps1" -ForegroundColor Yellow
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "好了。如意下次启动就会自动发现并拉起它，输入框的麦克风会变成边说边出字。" -ForegroundColor Green
exit 0
