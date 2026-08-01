param([ValidateSet('install','uninstall','start','stop','status')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
$AppHome = Join-Path $env:LOCALAPPDATA 'CodexCircuitResumer'
$InstallDir = Join-Path $AppHome 'app'
$Daemon = Join-Path $InstallDir 'CodexCircuitResumerDaemon.exe'
$TaskName = 'CodexCircuitResumer'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName = 'CodexCircuitResumer'
$PidFile = Join-Path $AppHome 'daemon.pid'

function Stop-Watcher {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    if (-not $PSCmdlet.ShouldProcess('CodexCircuitResumer', 'Stop watcher')) { return }
    if (Test-Path $PidFile) {
        $WatcherPid = [int](Get-Content $PidFile -Raw)
        Stop-Process -Id $WatcherPid -Force -ErrorAction SilentlyContinue
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -Filter "Name='CodexCircuitResumerDaemon.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -eq $Daemon } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Start-Watcher {
    [CmdletBinding(SupportsShouldProcess)]
    param()
    if (-not $PSCmdlet.ShouldProcess('CodexCircuitResumer', 'Start watcher')) { return }
    if (-not (Test-Path $Daemon)) { throw "找不到后台程序：$Daemon" }
    if (Test-Path $PidFile) {
        $ExistingPid = [int](Get-Content $PidFile -Raw)
        if (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue) { return }
    }
    $Process = Start-Process -FilePath $Daemon -WorkingDirectory $InstallDir -WindowStyle Hidden -PassThru
    New-Item -ItemType Directory -Force -Path $AppHome | Out-Null
    Set-Content -Path $PidFile -Value $Process.Id -Encoding ascii
}

switch ($Action) {
    'install' {
        if (-not (Test-Path $Daemon)) { throw "请从解压后的发行目录运行 Install.ps1" }
        $TaskCreated = $false
        try {
            $TaskAction = New-ScheduledTaskAction -Execute $Daemon -WorkingDirectory $InstallDir
            $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
            $Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 3650) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
            Register-ScheduledTask -TaskName $TaskName -Action $TaskAction -Trigger $Trigger -Settings $Settings -Description 'Codex 熔断续聊后台守望器' -Force | Out-Null
            $TaskCreated = $true
            Remove-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue
        } catch {
            New-Item -Path $RunKey -Force | Out-Null
            Set-ItemProperty -Path $RunKey -Name $RunName -Value ('"{0}"' -f $Daemon)
            Write-Warning '系统不允许创建计划任务，已改用当前用户登录自启。'
        }
        Start-Watcher
        if ($TaskCreated) {
            Write-Output '已设置开机自动运行：计划任务。'
        } else {
            Write-Output '已设置开机自动运行：当前用户登录自启。'
        }
    }
    'uninstall' {
        Stop-Watcher
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue
        Write-Output '已停止并移除开机自动运行。用户配置和日志仍保留。'
    }
    'start' { Start-Watcher; Write-Output '已启动。' }
    'stop' { Stop-Watcher; Write-Output '已停止。' }
    'status' {
        if (-not (Test-Path $Daemon)) { throw "找不到后台程序：$Daemon" }
        $Output = & $Daemon --status
        if ($LASTEXITCODE -ne 0) { throw "后台状态读取失败，退出码：$LASTEXITCODE" }
        $Output
    }
}
