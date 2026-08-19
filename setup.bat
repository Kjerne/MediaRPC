@echo off
echo ================================================
echo MediaRPC - Setup and Build
echo ================================================
echo.

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

py -m PyInstaller --onefile --noconsole --name MediaRPC --icon=MediaRPC_Active.ico --add-data "MediaRPC_Active.ico;." --add-data "MediaRPC_Inactive.ico;." mediarpc.py

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

if exist .env (
    copy .env dist\.env >nul
    echo [OK] .env copied
) else (
    echo [WARNING] .env not found! Copy .env.example to .env and fill it in.
)

copy MediaRPC_Active.ico dist\MediaRPC_Active.ico >nul 2>&1
copy MediaRPC_Inactive.ico dist\MediaRPC_Inactive.ico >nul 2>&1

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
