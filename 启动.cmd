@echo off
rem KyHelper Web launcher.
rem Keep this file ASCII-only: cmd reads bytes with the OEM codepage and
rem chokes on UTF-8 Chinese comments. Chinese docs are in README.md.
setlocal
cd /d "%~dp0"

set "PYEXE="
for %%P in (python.exe) do @if exist "%%~$PATH:P" set "PYEXE=%%~$PATH:P"

rem This machine's opencv lives in a separate python install.
if exist "G:\devTools\python\python.exe" set "PYEXE=G:\devTools\python\python.exe"
if exist "G:\project\py_repository\Python" set "PYTHONPATH=G:\project\py_repository\Python;%PYTHONPATH%"

if not defined PYEXE goto nopython

"%PYEXE%" -c "import cv2, numpy, yaml" 2>nul
if errorlevel 1 goto nodeps

"%PYEXE%" run.py
if errorlevel 1 pause
exit /b 0

:nopython
echo [!] Python not found. Install Python 3.9+ and run: pip install -r requirements.txt
pause
exit /b 1

:nodeps
echo [!] Missing dependencies. Run: pip install -r requirements.txt
pause
exit /b 1
