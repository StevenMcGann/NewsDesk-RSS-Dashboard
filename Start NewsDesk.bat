@echo off
rem Starts NewsDesk RSS Dashboard. Uses the Python bundled in this folder, or an installed one.
setlocal
rem pushd (not cd) so this also works from a \\server\share path.
pushd "%~dp0"

if exist "python\pythonw.exe" (
    start "" "python\pythonw.exe" "app\launcher.py"
    exit /b 0
)
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "app\launcher.py"
    exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "app\launcher.py"
    exit /b 0
)

echo NewsDesk needs Python, and this copy doesn't include it.
echo Ask for the full version: it has a folder named "python" inside.
pause
exit /b 1
