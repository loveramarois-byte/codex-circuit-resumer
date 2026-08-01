param([switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppHome = Join-Path $env:LOCALAPPDATA 'CodexCircuitResumer'
$InstallDir = Join-Path $AppHome 'app'
if (Test-Path (Join-Path $InstallDir 'control.ps1')) {
    & (Join-Path $InstallDir 'control.ps1') stop
}
Get-CimInstance Win32_Process -Filter "Name='CodexCircuitResumer.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq (Join-Path $InstallDir 'CodexCircuitResumer.exe') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item (Join-Path $Source '*') $InstallDir -Recurse -Force
$Shell = New-Object -ComObject WScript.Shell
$Desktop = [Environment]::GetFolderPath('Desktop')
New-Item -ItemType Directory -Force -Path $Desktop | Out-Null
$ShortcutPath = Join-Path $Desktop 'Codex 熔断续聊.lnk'
try {
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = Join-Path $InstallDir 'CodexCircuitResumer.exe'
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.Description = 'Codex 熔断续聊'
    $Shortcut.Save()
} catch {
    $ShortcutPath = Join-Path $Desktop 'Codex Circuit Resumer.lnk'
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = Join-Path $InstallDir 'CodexCircuitResumer.exe'
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.Description = 'Codex Circuit Resumer'
    $Shortcut.Save()
    Write-Warning '系统无法创建中文文件名，桌面快捷方式已使用英文名称。'
}
& (Join-Path $InstallDir 'control.ps1') install
if (-not $NoLaunch) { Start-Process (Join-Path $InstallDir 'CodexCircuitResumer.exe') }
Write-Output "安装完成：$InstallDir"
