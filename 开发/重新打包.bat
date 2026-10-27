@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Rebuilding AgentSync.exe ...
echo.

rem 1) 优先用 PATH 里的 python；2) 没有则退回本机虚拟环境
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
        set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
    )
)

"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is missing. Install it first:
    echo     "%PY%" -m pip install pyinstaller
    echo.
    pause
    exit /b 1
)

"%PY%" -m PyInstaller --onefile --name AgentSync --console --noconfirm --clean --distpath ".." --workpath build --specpath build --paths . --exclude-module tkinter --exclude-module unittest --exclude-module pydoc launcher.py
if errorlevel 1 (echo BUILD FAILED) else (echo Done -^> ..\AgentSync.exe)
pause
