@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "EDITOR_PYTHON=%~dp0runtime\python\Scripts\python.exe"
if not exist "%EDITOR_PYTHON%" (
    echo 找不到本机剪辑 Python 环境。请检查 runtime\python。
    pause
    exit /b 1
)
"%EDITOR_PYTHON%" "%~dp0app\local_launcher.py" %*
if errorlevel 1 (
    pause
    exit /b 1
)
