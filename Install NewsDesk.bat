@echo off
rem Installs NewsDesk RSS Dashboard for this user: copies it to
rem %LOCALAPPDATA%\Programs\NewsDesk and adds desktop and Start menu shortcuts.
rem Nothing is registered with Windows. No admin rights needed.
setlocal
title Install NewsDesk RSS Dashboard
pushd "%~dp0"
if not exist "python\python.exe" (
    echo This copy of NewsDesk has no bundled Python, so it can't be installed.
    echo Download the full NewsDesk zip: it has a folder named "python" inside.
    pause
    exit /b 1
)
"python\python.exe" -B "app\installer.py" %*
set "result=%errorlevel%"
popd
echo.
pause
exit /b %result%
