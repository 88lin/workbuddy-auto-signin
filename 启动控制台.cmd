@echo off
cd /d "%~dp0"
set "PYW=F:\Python\versions\cpython-3.12-windows-x86_64-none\pythonw.exe"
if not exist "%PYW%" set "PYW=python.exe"
start "" /min "%PYW%" "%~dp0hub.py"
echo Started. Dashboard: http://127.0.0.1:8765/
