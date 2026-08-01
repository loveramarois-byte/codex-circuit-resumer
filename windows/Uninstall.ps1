$ErrorActionPreference = 'Stop'
$AppHome = Join-Path $env:LOCALAPPDATA 'CodexCircuitResumer'
$InstallDir = Join-Path $AppHome 'app'
if (Test-Path (Join-Path $InstallDir 'control.ps1')) { & (Join-Path $InstallDir 'control.ps1') uninstall }
$Desktop = [Environment]::GetFolderPath('Desktop')
Remove-Item (Join-Path $Desktop 'Codex 熔断续聊.lnk') -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Desktop 'Codex Circuit Resumer.lnk') -Force -ErrorAction SilentlyContinue
Write-Output "后台与快捷方式已移除。如需彻底清理，可手动删除：$AppHome"
