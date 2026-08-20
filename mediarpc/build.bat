@echo off
echo ================================================
echo Building MediaRPC (quick rebuild)
echo ================================================
echo.

REM Run from the repo root (parent of this script's mediarpc\ folder) so the
REM mediarpc package is importable and dist\ / build\ land at the root.
cd /d "%~dp0.."

py -m PyInstaller --onefile --noconsole --name MediaRPC --icon=mediarpc/Images/MediaRPC_Active.ico --add-data "mediarpc/Images/MediaRPC_Active.ico;Images" --add-data "mediarpc/Images/MediaRPC_Inactive.ico;Images" mediarpc/run_mediarpc.py

echo.
echo Copying files to dist...
REM .env may live in mediarpc\ (dev layout) or at the root (distribution layout).
if exist mediarpc\.env (
    copy mediarpc\.env dist\.env >nul 2>&1
) else if exist .env (
    copy .env dist\.env >nul 2>&1
)

echo.
echo Done! Run: dist\MediaRPC.exe
pause
