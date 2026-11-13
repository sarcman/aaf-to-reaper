@echo off
rem AAF to Reaper - double-click, or drag AAF files onto this file.
setlocal
set "HERE=%~dp0"
set PYTHONDONTWRITEBYTECODE=1
set PYTHONIOENCODING=utf-8

where pyw >nul 2>nul && (start "" pyw -3 "%HERE%AAF to Reaper.pyw" %* & exit /b 0)
where pythonw >nul 2>nul && (start "" pythonw "%HERE%AAF to Reaper.pyw" %* & exit /b 0)

echo.
echo  Python 3 was not found on this computer.
echo.
echo  1. Download it from https://www.python.org/downloads/windows/
echo  2. In the installer, tick "Add python.exe to PATH" and keep "tcl/tk" selected.
echo  3. Then double-click "AAF to Reaper.bat" again.
echo.
pause
exit /b 1
