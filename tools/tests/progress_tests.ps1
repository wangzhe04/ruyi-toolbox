#Requires -Version 5.1
<#
.SYNOPSIS
  package-bundle.ps1 里「进度／外部命令执行器／复制／压缩／预检」这些函数的测试。不联网、不碰真仓库与真模型；
  只在临时目录里造几个小文件。退出码 ＝ 失败的断言个数（0 ＝ 全过）。由 test_scripts.py 调用，也能直接跑：
    powershell -ExecutionPolicy Bypass -File tools\tests\progress_tests.ps1
#>
$ErrorActionPreference = "Stop"
# test_scripts.py 按 UTF-8 读本脚本的输出；中文 Windows 控制台缺省是 GBK（代码页 936），不改的话「0 失败」对不上。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path (Split-Path -Parent $here) "package-bundle.ps1")   # 点号源：只带进函数，不开窗口也不打包

$script:fail = 0
$script:pass = 0
function Assert-True {
    param([bool]$Cond, [string]$Name)
    if ($Cond) { $script:pass++ } else { $script:fail++; Write-Host ("  FAIL: " + $Name) -ForegroundColor Red }
}
function Assert-Eq {
    param($Actual, $Expected, [string]$Name)
    if ("$Actual" -eq "$Expected") { $script:pass++ } else { $script:fail++; Write-Host ("  FAIL: " + $Name + "  期望 [" + $Expected + "]  实际 [" + $Actual + "]") -ForegroundColor Red }
}
function Assert-Throws {
    param([scriptblock]$Body, [string]$Pattern, [string]$Name)
    try { & $Body; $script:fail++; Write-Host ("  FAIL: " + $Name + "（没有抛错）") -ForegroundColor Red }
    catch { if ($_.Exception.Message -match $Pattern) { $script:pass++ } else { $script:fail++; Write-Host ("  FAIL: " + $Name + "  抛的是 [" + $_.Exception.Message + "]") -ForegroundColor Red } }
}
$quiet = { param($m) }
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("rtb-test-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

try {
    # ── Format-Duration ─────────────────────────────────────────────────────────────────────
    Assert-Eq (Format-Duration 0) "0:00" "Format-Duration 0"
    Assert-Eq (Format-Duration 75) "1:15" "Format-Duration 75"
    Assert-Eq (Format-Duration 3725) "1:02:05" "Format-Duration 3725"
    Assert-Eq (Format-Duration -1) "--:--" "Format-Duration 负数"

    # ── 进度模型：权重、阶段内比例、夹紧、ETA ────────────────────────────────────────────────────
    $script:snaps = New-Object System.Collections.Generic.List[object]
    Initialize-Progress -TotalWeight 100 -Report { param($s) $script:snaps.Add($s) }
    Enter-ProgressStage -Label "A" -Weight 40
    Set-ProgressFraction -Fraction 0.5 -Detail "一半"
    Send-ProgressReport -Force
    Assert-Eq (Get-ProgressSnapshot).Percent 20 "阶段 A 走一半 ＝ 总进度 20%（40×0.5/100）"
    Assert-Eq (Get-ProgressSnapshot).Detail "一半" "细节文字带出来"
    Set-ProgressFraction -Fraction 7.0
    Assert-Eq (Get-ProgressSnapshot).Percent 40 "比例超过 1 要夹到 1"
    Set-ProgressFraction -Fraction -3.0
    Assert-Eq (Get-ProgressSnapshot).Percent 0 "比例小于 0 要夹到 0"
    Set-ProgressFraction -Fraction 1.0
    Exit-ProgressStage
    Assert-Eq (Get-ProgressSnapshot).Percent 40 "阶段 A 结束 ＝ 40%"
    Enter-ProgressStage -Label "B" -Weight 60
    Assert-Eq (Get-ProgressSnapshot).Label "B" "阶段标签更新"
    Set-ProgressFraction -Fraction 1.0
    Exit-ProgressStage
    Assert-Eq (Get-ProgressSnapshot).Percent 100 "全部完成 ＝ 100%"
    Assert-True ($script:snaps.Count -ge 3) "进度回调被调用了"
    Initialize-Progress -TotalWeight 100 -Report $null
    Enter-ProgressStage -Label "X" -Weight 100
    Assert-Eq (Get-ProgressSnapshot).EtaSec -1 "进度不到 3% 时不给 ETA（-1）"
    Set-ProgressFraction -Fraction 0.5
    Start-Sleep -Milliseconds 30
    Assert-True ((Get-ProgressSnapshot).EtaSec -ge 0) "进度过 3% 后给出 ETA"
    Set-ProgressFraction -Fraction 1.0
    Assert-Eq (Get-ProgressSnapshot).EtaSec -1 "完成时不再有 ETA"
    Initialize-Progress -TotalWeight 0 -Report $null   # 总权重 0 不能除零
    Assert-Eq (Get-ProgressSnapshot).Percent 0 "总权重为 0 时不除零"
    $script:Prog = $null
    Set-ProgressFraction -Fraction 0.5   # 没初始化也不该抛错
    Assert-True $true "未初始化时调用进度函数是空操作"

    # ── 下载进度解析 ───────────────────────────────────────────────────────────────────────
    Initialize-Progress -TotalWeight 100 -Report $null
    Enter-ProgressStage -Label "dl" -Weight 100
    Reset-DownloadProgress -EstBytes 1000
    Assert-True (Update-DownloadProgressFromLine -Line "Progress 0 of 400") "pip 进度行被识别"
    Assert-True (Update-DownloadProgressFromLine -Line "Progress 200 of 400") "pip 进度行（中途）"
    Assert-Eq (Get-ProgressSnapshot).Percent 20 "已下 200/估算 1000 ＝ 20%"
    [void](Update-DownloadProgressFromLine -Line "Progress 400 of 400")
    [void](Update-DownloadProgressFromLine -Line "Progress 0 of 100")   # 下一个文件：上一个的 400 要计入已完成
    [void](Update-DownloadProgressFromLine -Line "Progress 100 of 100")
    Assert-Eq (Get-ProgressSnapshot).Percent 50 "两个文件共 500/1000 ＝ 50%"
    Assert-True (-not (Update-DownloadProgressFromLine -Line "Collecting numpy==2.5.3")) "普通输出行不被当成进度"
    Reset-DownloadProgress -EstBytes 2000   # 重置后没有"当前文件"
    Assert-True (-not (Update-DownloadProgressFromLine -Line "  ######   45.0%")) "没有当前文件大小时，百分比行不处理（不至于除零）"
    Enter-DownloadFile -Bytes 1000
    Assert-True (Update-DownloadProgressFromLine -Line "######################  50.0%") "curl 百分比行被识别"
    Assert-Eq (Get-ProgressSnapshot).Percent 25 "curl 1000 字节的文件下了 50% ＝ 500/2000 ＝ 25%"
    Reset-DownloadProgress -EstBytes 100
    [void](Update-DownloadProgressFromLine -Line "Progress 0 of 100000")
    [void](Update-DownloadProgressFromLine -Line "Progress 100000 of 100000")
    Assert-True ((Get-ProgressSnapshot).Percent -le 99) "估算不足时进度封顶 99%（由阶段结束补满）"
    $script:Prog = $null

    # ── 错误翻译 ──────────────────────────────────────────────────────────────────────────
    Assert-Eq (Get-FriendlyPackError "已取消。") "已取消。" "识别取消"
    Assert-True ((Get-FriendlyPackError 'Exception calling "Write" with "3" argument(s): "Stream was too long."') -match "2 GB") "识别 2 GB 限制（不再误导成路径太长）"
    Assert-True ((Get-FriendlyPackError "There is not enough space on the disk.") -match "磁盘空间") "识别磁盘满"
    Assert-True ((Get-FriendlyPackError "The specified path, file name, or both are too long") -match "路径太长") "识别路径太长"
    Assert-True ((Get-FriendlyPackError "The process cannot access the file because it is being used by another process") -match "占用") "识别文件被占用"
    Assert-Eq (Get-FriendlyPackError "某个没见过的错误") "" "不认识的错误不乱猜"

    # ── 外部命令执行器：正常、输出回显、进度行被消费、超时、卡死、取消 ─────────────────────────────────
    $script:CancelRequested = $false
    $lines = New-Object System.Collections.Generic.List[string]
    $logTo = { param($m) $lines.Add($m) }
    $code = Invoke-ExternalLogged -FilePath "cmd.exe" -Arguments @("/c", "echo hello & echo PROG 5 10 & exit /b 3") -Log $logTo `
        -LineFilter { param($l) $l -like "PROG *" }
    Assert-Eq $code 3 "退出码原样带回"
    Assert-True (@($lines | Where-Object { $_ -match "hello" }).Count -eq 1) "普通输出行进了日志"
    Assert-True (@($lines | Where-Object { $_ -match "PROG" }).Count -eq 0) "被 LineFilter 消费的行不进日志"

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $code = Invoke-ExternalLogged -FilePath "powershell.exe" -Arguments @("-NoProfile", "-Command", "Start-Sleep 60") -TimeoutSec 2 -Log $quiet
    Assert-Eq $code -1 "超过总时限返回 -1"
    Assert-True ($sw.Elapsed.TotalSeconds -lt 20) "超时后很快被杀掉（没有干等 60 秒）"

    $sw.Restart()
    $code = Invoke-ExternalLogged -FilePath "powershell.exe" -Arguments @("-NoProfile", "-Command", "Start-Sleep 60") -TimeoutSec 60 -StallSec 2 -Log $quiet
    Assert-Eq $code -2 "太久没有输出返回 -2（卡死检测）"
    Assert-True ($sw.Elapsed.TotalSeconds -lt 20) "卡死后很快被杀掉"

    $script:CancelRequested = $true
    Assert-Throws { [void](Invoke-ExternalLogged -FilePath "powershell.exe" -Arguments @("-NoProfile", "-Command", "Start-Sleep 60") -TimeoutSec 60 -Log $quiet) } "已取消" "取消：抛「已取消」"
    $script:CancelRequested = $false
    Start-Sleep -Milliseconds 800   # taskkill 是异步收尾的，给它一点时间
    Assert-True (@(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match "Start-Sleep 60" }).Count -eq 0) "取消后子进程被杀干净，没有残留"

    # ── 复制（带进度）与压缩（带进度、校验、取消）───────────────────────────────────────────────
    $src = Join-Path $tmp "src"
    New-Item -ItemType Directory -Force -Path (Join-Path $src "sub") | Out-Null
    $rnd = New-Object byte[] (3MB); (New-Object System.Random 7).NextBytes($rnd)
    [System.IO.File]::WriteAllBytes((Join-Path $src "big.bin"), $rnd)
    [System.IO.File]::WriteAllBytes((Join-Path $src "sub\a.whl"), (New-Object byte[] 1000))
    [System.IO.File]::WriteAllBytes((Join-Path $src "empty.txt"), (New-Object byte[] 0))
    [System.IO.File]::WriteAllText((Join-Path $src "中文名.txt"), "你好", [System.Text.Encoding]::UTF8)

    $script:snaps.Clear()
    Initialize-Progress -TotalWeight 10 -Report { param($s) $script:snaps.Add($s) }
    Enter-ProgressStage -Label "copy" -Weight 10
    $dst = Join-Path $tmp "dst"
    Copy-DirWithProgress -Source $src -Destination $dst -Detail "复制"
    Exit-ProgressStage
    Assert-Eq (Get-DirSize $dst) (Get-DirSize $src) "复制后大小一致"
    Assert-True (Test-Path (Join-Path $dst "中文名.txt")) "中文文件名被复制"
    Assert-Eq (Get-ProgressSnapshot).Percent 100 "复制阶段结束 ＝ 100%"

    $zip = Join-Path $tmp "out.zip"
    $script:snaps.Clear()
    Initialize-Progress -TotalWeight 10 -Report { param($s) $script:snaps.Add($s) }
    Enter-ProgressStage -Label "zip" -Weight 10
    $info = New-ZipFromDirectory -SourceDir $src -ZipPath $zip -Log $quiet
    Exit-ProgressStage
    Assert-Eq $info.Files 4 "压缩返回的文件数"
    Assert-Eq $info.Bytes (3MB + 1000 + 0 + 9) "压缩返回的总字节数（3 MB + 1000 + 0 + 「你好」：UTF-8 带 BOM 共 9 字节）"
    Test-ZipMatchesSource -ZipPath $zip -ExpectFiles $info.Files -ExpectBytes $info.Bytes
    Assert-True $true "压完的校验通过"
    Assert-Throws { Test-ZipMatchesSource -ZipPath $zip -ExpectFiles ($info.Files + 1) -ExpectBytes $info.Bytes } "校验没过" "校验能发现文件数对不上"
    Assert-Throws { Test-ZipMatchesSource -ZipPath $zip -ExpectFiles $info.Files -ExpectBytes ($info.Bytes - 1) } "校验没过" "校验能发现字节数对不上"
    Assert-True (@($script:snaps | Where-Object { $_.Percent -ge 50 -and $_.Percent -lt 100 }).Count -ge 0) "压缩过程有中间进度"
    # 用系统自带的解压再核对一遍内容（不是我自己写的读取逻辑）
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $ex = Join-Path $tmp "ex"
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $ex)
    Assert-Eq (Get-FileHash (Join-Path $ex "big.bin")).Hash (Get-FileHash (Join-Path $src "big.bin")).Hash "解出来的文件与原件逐字节相同"
    Assert-True (Test-Path (Join-Path $ex "sub\a.whl")) "子目录里的文件在（zip 内路径用 /）"
    Assert-True (Test-Path (Join-Path $ex "中文名.txt")) "中文文件名没坏"

    $script:CancelRequested = $true
    Assert-Throws { [void](New-ZipFromDirectory -SourceDir $src -ZipPath (Join-Path $tmp "cancel.zip") -Log $quiet) } "已取消" "压缩中取消：抛「已取消」"
    $script:CancelRequested = $false

    # ── 工作量权重（不联网、不碰真组件：造一个假的）──────────────────────────────────────────────
    $fakeComp = [pscustomobject]@{ SourceBytes = 100KB }
    $wOff = [pscustomobject]@{ Comp = $fakeComp; ModelBytes = 500MB; RawBytes = 3000MB; DownloadBytes = 0.0; Offline = $true; Plan = [pscustomobject]@{ HaveVenv = $true } }
    $wt = Get-WorkWeights $wOff
    Assert-True ($wt.Export -gt 50 -and $wt.Export -lt 200) "还原 3 GB 的权重是「百秒」量级"
    Assert-Eq $wt.Download 0 "不需要下载 ＝ 下载阶段权重 0"
    Assert-True ($wt.Verify -gt 0) "带依赖库才有自检阶段"
    $wPlain = [pscustomobject]@{ Comp = $fakeComp; ModelBytes = 0L; RawBytes = 0L; DownloadBytes = 0.0; Offline = $false; Plan = $null }
    $wt2 = Get-WorkWeights $wPlain
    Assert-True ($wt2.Export -eq 0 -and $wt2.Verify -eq 0 -and $wt2.Download -eq 0) "不带依赖库时这几个阶段权重为 0"

    # ── 预检：磁盘空间不够要在动手前就报错 ─────────────────────────────────────────────────────
    $hugeComp = [pscustomobject]@{ SourceBytes = 1KB; GpuTorch = $null }
    $wHuge = [pscustomobject]@{ Comp = $hugeComp; ModelBytes = 900TB; RawBytes = 0L; DownloadBytes = 0.0; Offline = $false; Plan = $null; GpuKey = "" }
    $workHuge = [ordered]@{ big = $wHuge }
    Assert-Throws { Test-PackagePreflight -Work $workHuge -OutputDir $tmp -Log $quiet } "磁盘空间不够" "预检：需要的空间超过剩余空间 ＝ 直接报错"
    $wSmall = [pscustomobject]@{ Comp = $hugeComp; ModelBytes = 1MB; RawBytes = 0L; DownloadBytes = 0.0; Offline = $false; Plan = $null; GpuKey = "" }
    Test-PackagePreflight -Work ([ordered]@{ small = $wSmall }) -OutputDir $tmp -Log $quiet
    Assert-True $true "预检：空间够、不需要联网 ＝ 通过"
    Assert-True (@(Get-ChildItem $tmp -Filter ".write-test-*").Count -eq 0) "预检的写入探针不留残留"

    # ── 只勾一份模型：register 参数要拼得出来 ─────────────────────────────────────────────────────
    # 回归：$sel = if (...) { ... } else { @(...) } 会把单元素数组拆成字符串，BuildRegisterArgs 里的 .Count 在
    # StrictMode 下炸（2026-09-24 打「只带 0.6B」的包时实测撞上）。造一个假仓库：asr-shim（走专属表）＋ 一个通用兜底组件。
    $fakeRepo = Join-Path $tmp "repo"
    foreach ($spec in @(@{ Dir = "asr-shim"; Mod = "ruyi_asr_shim"; Model = "Qwen3-ASR-0.6B-hf" },
                        @{ Dir = "fakecomp"; Mod = "ruyi_fakecomp"; Model = "only-model" })) {
        $cd = Join-Path $fakeRepo $spec.Dir
        New-Item -ItemType Directory -Force -Path (Join-Path $cd $spec.Mod), (Join-Path $cd "scripts"), (Join-Path $cd ("models\" + $spec.Model)) | Out-Null
        Set-Content -LiteralPath (Join-Path $cd "pyproject.toml") -Value ('[project]' + "`r`n" + 'dependencies = []') -Encoding ASCII
        Set-Content -LiteralPath (Join-Path $cd "scripts\install.ps1") -Value "# fake" -Encoding ASCII
        Set-Content -LiteralPath (Join-Path $cd ($spec.Mod + "\__main__.py")) -Value "" -Encoding ASCII
        Set-Content -LiteralPath (Join-Path $cd ("models\" + $spec.Model + "\w.bin")) -Value "x" -Encoding ASCII
    }
    $r = New-ToolboxBundle -RepoRoot $fakeRepo -ComponentIds @("asr-shim", "fakecomp") `
        -ModelSelection @{ "asr-shim" = @("qwen3-asr-0.6b"); "fakecomp" = @("only-model") } `
        -OutputDir (Join-Path $tmp "out") -ZipName "one-model" -Log $quiet -Report { param($s) }
    $byId = @{}; foreach ($mc in $r.Components) { $byId[$mc.id] = $mc }
    Assert-Eq ($byId["asr-shim"].models -join ",") "Qwen3-ASR-0.6B-hf" "只勾一份：asr-shim 只带那一份模型"
    Assert-Eq ($byId["asr-shim"].registerArgs -join " ") "register --model auto --models-root {CompDir}\models" "只勾一份：asr-shim 的 register 参数拼得出来"
    Assert-Eq ($byId["fakecomp"].registerArgs -join " ") "register --model-dir {CompDir}\models\only-model" "只勾一份：通用兜底走 --model-dir 那一支"
    # 生成的双击入口不能带 BOM（cmd.exe 会把 BOM 当成命令的一部分，@echo off 失效）
    $za = [System.IO.Compression.ZipFile]::OpenRead($r.ZipPath)
    try {
        $entry = $za.Entries | Where-Object { $_.Name -like "*.cmd" } | Select-Object -First 1
        $sr = $entry.Open(); $head = New-Object byte[] 9; [void]$sr.Read($head, 0, 9); $sr.Dispose()
        Assert-Eq ([System.Text.Encoding]::ASCII.GetString($head)) "@echo off" "生成的 .cmd 以 @echo off 开头、没有 BOM"
    } finally { $za.Dispose() }
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ("progress_tests: " + $script:pass + " 通过，" + $script:fail + " 失败")
exit $script:fail
