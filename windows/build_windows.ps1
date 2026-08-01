param([string]$OutputDir = "$PSScriptRoot\..\dist\windows")
$ErrorActionPreference = 'Stop'
$Project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$BuildTemp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } elseif ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
$Stage = Join-Path $OutputDir 'CodexCircuitResumer-Windows-x64'
Remove-Item $OutputDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
python -m pip install --disable-pip-version-check --quiet pyinstaller==6.15.0
python -m PyInstaller --noconfirm --clean --onefile --name CodexCircuitResumerDaemon (Join-Path $Project 'src\daemon.py') --distpath $Stage --workpath (Join-Path $BuildTemp 'ccr-daemon-work') --specpath (Join-Path $BuildTemp 'ccr-spec')
python -m PyInstaller --noconfirm --clean --onefile --windowed --name CodexCircuitResumer (Join-Path $Project 'windows\gui.py') --distpath $Stage --workpath (Join-Path $BuildTemp 'ccr-gui-work') --specpath (Join-Path $BuildTemp 'ccr-spec')
Copy-Item (Join-Path $Project 'windows\control.ps1'), (Join-Path $Project 'windows\Install.ps1'), (Join-Path $Project 'windows\Install.cmd'), (Join-Path $Project 'windows\Uninstall.ps1'), (Join-Path $Project 'config.example.json') $Stage
Get-ChildItem $Stage -File | Get-FileHash -Algorithm SHA256 | ForEach-Object { "$($_.Hash.ToLower())  $($_.Path | Split-Path -Leaf)" } | Set-Content (Join-Path $Stage 'SHA256SUMS.txt') -Encoding ascii
Compress-Archive -Path $Stage -DestinationPath (Join-Path $OutputDir 'CodexCircuitResumer-Windows-x64.zip') -CompressionLevel Optimal
Get-FileHash (Join-Path $OutputDir 'CodexCircuitResumer-Windows-x64.zip') -Algorithm SHA256 | ForEach-Object { "$($_.Hash.ToLower())  CodexCircuitResumer-Windows-x64.zip" } | Set-Content (Join-Path $OutputDir 'CodexCircuitResumer-Windows-x64.zip.sha256') -Encoding ascii
Write-Output $OutputDir
