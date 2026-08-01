param([Parameter(Mandatory=$true)][string]$Stage)
$ErrorActionPreference = 'Stop'
$Runtime = Join-Path $env:RUNNER_TEMP 'CodexCircuitResumerSmoke'
Remove-Item $Runtime -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
$env:CODEX_CIRCUIT_RESUMER_HOME = $Runtime
$StatusJson = & (Join-Path $Stage 'CodexCircuitResumerDaemon.exe') --status
if ($LASTEXITCODE -ne 0) { throw "发行版状态命令失败，退出码：$LASTEXITCODE" }
$StatusJson | ConvertFrom-Json | Out-Null
& (Join-Path $Stage 'CodexCircuitResumerDaemon.exe') --once --dry-run
if ($LASTEXITCODE -ne 0) { throw "发行版单轮守望失败，退出码：$LASTEXITCODE" }
& (Join-Path $Stage 'Install.ps1') -NoLaunch
Start-Sleep -Seconds 5
$InstallDir = Join-Path $env:LOCALAPPDATA 'CodexCircuitResumer\app'
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPaths = @(
    (Join-Path $Desktop 'Codex 熔断续聊.lnk'),
    (Join-Path $Desktop 'Codex Circuit Resumer.lnk')
)
if (-not ($ShortcutPaths | Where-Object { Test-Path $_ })) { throw '桌面快捷方式未创建' }
$ScheduledTask = Get-ScheduledTask -TaskName 'CodexCircuitResumer' -ErrorAction SilentlyContinue
$RunValue = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue).CodexCircuitResumer
if (-not $ScheduledTask -and -not $RunValue) { throw '开机自启未创建' }
if ($ScheduledTask -and $RunValue) { throw '计划任务和注册表登录自启同时存在，会导致重复启动' }
$Status = (& (Join-Path $InstallDir 'control.ps1') status | ConvertFrom-Json)
if (-not $Status.healthy) { throw '后台心跳不健康' }
& (Join-Path $InstallDir 'control.ps1') stop
& (Join-Path $Stage 'Uninstall.ps1')
if (Get-ScheduledTask -TaskName 'CodexCircuitResumer' -ErrorAction SilentlyContinue) { throw '卸载后计划任务仍存在' }
if ((Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue).CodexCircuitResumer) { throw '卸载后登录自启仍存在' }
if ($ShortcutPaths | Where-Object { Test-Path $_ }) { throw '卸载后桌面快捷方式仍存在' }
Write-Output 'Windows smoke test passed.'
