@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1"
if errorlevel 1 (
  echo.
  echo 安装失败。请把上面的错误截图发到 Issue，或重新解压后再运行本文件。
  pause
  exit /b 1
)
echo.
echo 安装完成。桌面已经创建“Codex 熔断续聊”快捷方式。
pause
