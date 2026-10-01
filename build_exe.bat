@echo off
rem Builds a standalone SynchroReportExporter.exe (no Python needed on the PCs that run it).
rem Run this ON WINDOWS, from the folder that holds the .py files.
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller pywinauto || goto :fail

rem ---- ONEFILE: one .exe, easiest to hand out (a bit slower to start; more likely to trip antivirus)
python -m PyInstaller --noconfirm --clean --noconsole --onefile ^
  --name SynchroReportExporter ^
  --hidden-import synchro_batch ^
  --collect-submodules pywinauto --collect-submodules comtypes ^
  synchro_exporter_ui.py || goto :fail

echo.
echo Built:  dist\SynchroReportExporter.exe
echo.
echo If Windows Defender flags the single .exe, rebuild as a folder instead: replace --onefile with --onedir
echo and share the whole dist\SynchroReportExporter folder (zip it).
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
