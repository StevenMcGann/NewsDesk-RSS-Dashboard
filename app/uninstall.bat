@echo off
rem Uninstall NewsDesk RSS Dashboard. The installer copies this file into the install
rem folder as "Uninstall NewsDesk.bat". It uses no literal paths (only %~dp0), so it
rem works for any user name, and it only acts on a folder the installer created.
setlocal
title Uninstall NewsDesk RSS Dashboard
pushd "%~dp0"
if not exist "install.json" (
    echo This copy of NewsDesk wasn't installed, so there is nothing to uninstall.
    echo To remove it, close NewsDesk and delete this folder.
    pause
    exit /b 1
)
echo This removes NewsDesk RSS Dashboard and its desktop and Start menu shortcuts.
echo.
set "answer="
set /p "answer=Uninstall NewsDesk? (y/n) "
if /i not "%answer%"=="y" exit /b 0
rem Checks that NewsDesk isn't running from here (exit code 2), then removes the shortcuts.
"python\python.exe" -B "app\installer.py" --remove-shortcuts
if errorlevel 1 (
    pause
    exit /b 1
)
echo.
echo Your feeds, settings and reading history are in %APPDATA%\NewsDesk.
set "answer="
set /p "answer=Delete them too? (y/n) "
if /i "%answer%"=="y" rmdir /s /q "%APPDATA%\NewsDesk"
set "folder=%~dp0"
popd
cd /d "%TEMP%"
rem "(goto)" lets this script delete the folder it lives in.
(goto) 2>nul & rmdir /s /q "%folder%" & echo NewsDesk has been removed. & pause
