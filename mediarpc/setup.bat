@echo off
echo ================================================
echo MediaRPC - Setup and Build
echo ================================================
echo.

REM Run from the repo root (parent of this script's mediarpc\ folder).
cd /d "%~dp0.."

echo [0/3] Updating pip...
python -m pip install --upgrade pip setuptools wheel

echo.
echo [1/3] Installing Python dependencies...
echo.

pip install --only-binary=:all: -r requirements.txt

if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies!
    echo Try installing Microsoft C++ Build Tools if this continues.
    echo https://visualstudio.microsoft.com/visual-cpp-build-tools/
    echo.
    pause
    exit /b 1
)

echo.
echo ================================================
echo [2/3] Building MediaRPC executable...
echo ================================================
echo.

py -m PyInstaller --onefile --noconsole --name MediaRPC --icon=mediarpc/Images/MediaRPC_Active.ico --add-data "mediarpc/Images/MediaRPC_Active.ico;Images" --add-data "mediarpc/Images/MediaRPC_Inactive.ico;Images" mediarpc/run_mediarpc.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed!
    pause
    exit /b 1
)

echo.
echo ================================================
echo [3/3] Copying files to dist folder...
echo ================================================
echo.

REM .env may live in mediarpc\ (dev layout) or at the root (distribution layout).
if exist mediarpc\.env (
    copy mediarpc\.env dist\.env >nul
    echo [OK] .env copied
) else if exist .env (
    copy .env dist\.env >nul
    echo [OK] .env copied
) else (
    echo [WARNING] .env not found! Copy .env.example to .env and fill it in.
)

echo.
echo ================================================
echo Build complete!
echo ================================================
echo.
echo Your exe is at: dist\MediaRPC.exe
echo.
dir /b dist
echo.
pause