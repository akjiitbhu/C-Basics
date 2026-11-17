@echo off
REM Build EasyTally.exe (and optionally the installer) on Windows.
REM Needs Python 3.9+ from python.org (includes Tkinter). Run from the easy_tally folder.
python -m pip install --upgrade pip || exit /b 1
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --windowed --name EasyTally ^
  --collect-all pdfplumber --collect-all pdfminer --collect-all pypdfium2 --collect-all reportlab --collect-all openpyxl --add-data "easytally/web/static;easytally/web/static" run.py || exit /b 1
echo.
echo Built: dist\EasyTally\EasyTally.exe
where iscc >nul 2>nul
if %errorlevel%==0 (
  iscc installer\EasyTally.iss && echo Installer: dist-installer\EasyTally-Setup.exe
) else (
  echo Install Inno Setup 6 and run: iscc installer\EasyTally.iss   ^(to make the one-click installer^)
)
