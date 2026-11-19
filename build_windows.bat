@echo off
REM Builds BPReader.exe from this source folder.
REM Run this on Windows, from inside the bpapp folder, by double-clicking it
REM or running: build_windows.bat
REM
REM Needs: Python 3.10+ installed from python.org (check "Add to PATH"
REM during install), and internet access for this one build step only.
REM The resulting .exe runs fully offline.

setlocal
cd /d "%~dp0"

echo ==> Checking Python
python --version
if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo and make sure to check "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

echo ==> Creating build virtual environment
if not exist build_venv (
    python -m venv build_venv
)
call build_venv\Scripts\activate.bat

echo ==> Installing dependencies
python -m pip install --quiet --upgrade pip
pip install --quiet flask bleak terminaltables pyinstaller

echo ==> Fetching omblepy engine
if not exist omblepy_src (
    git --version >nul 2>&1
    if errorlevel 1 (
        echo Git is not installed. Download it from https://git-scm.com/download/win
        echo or, if you'd rather not install Git, download
        echo https://github.com/userx14/omblepy/archive/refs/heads/main.zip
        echo and unzip it here as a folder named "omblepy_src".
        pause
        exit /b 1
    )
    git clone --depth 1 https://github.com/userx14/omblepy.git omblepy_src
    rmdir /s /q omblepy_src\.git
)

echo ==> Building BPReader.exe (this can take a minute or two)
pyinstaller --noconfirm --onefile --windowed --name BPReader ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --add-data "omblepy_src;omblepy" ^
    --collect-all bleak ^
    --hidden-import terminaltables ^
    server.py

echo.
echo Done. Find it at: dist\BPReader.exe
echo Double-click it to run — it opens your browser automatically.
pause
