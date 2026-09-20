#Requires -Version 5.1
<#
.SYNOPSIS
  用 Windows 自带的语音合成造一段 16 kHz 单声道 WAV，用来冒烟测试。

.DESCRIPTION
  样例音频【不进 git】（仓库里只放这个生成脚本）。合成出来的是 System.Speech 的默认音色，
  中文要机器上装了中文语音包才有；没有就换 -Text 用英文，或者自己录一段。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\make-sample.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\make-sample.ps1 -Text "Hello there." -Out .\samples\en.wav
#>
[CmdletBinding()]
param(
    [string]$Text = "你好，今天下午三点开会，请帮我记一下。",
    [string]$Out = "",
    [switch]$ListVoices
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

Add-Type -AssemblyName System.Speech

if ($ListVoices) {
    $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
    foreach ($v in $s.GetInstalledVoices()) {
        $info = $v.VoiceInfo
        Write-Host ($info.Name + "  [" + $info.Culture.Name + "]")
    }
    $s.Dispose()
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Out)) {
    $Out = Join-Path $root "samples\sample.wav"
}
$Out = [System.IO.Path]::GetFullPath($Out)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Out) | Out-Null

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    # 16 kHz / 16 bit / 单声道 —— 与如意麦克风发给 shim 的那一段形状一致。
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($Out, $fmt)
    $synth.Speak($Text)
}
finally {
    $synth.Dispose()
}

$len = (Get-Item $Out).Length
Write-Host ("造好了：" + $Out + "（" + $len + " 字节）") -ForegroundColor Green
Write-Host "注意：samples\ 被 .gitignore 挡住，不会进仓库。"
exit 0
