@echo off
echo ================================================
echo Building MediaRPC (quick rebuild)
echo ================================================
echo.

py -m PyInstaller --onefile --noconsole --name MediaRPC --icon=MediaRPC_Active.ico --add-data "MediaRPC_Active.ico;." --add-data "MediaRPC_Inactive.ico;." mediarpc.py

echo.
echo Copying files to dist...
copy .env dist\.env >nul 2>&1
copy MediaRPC_Active.ico dist\MediaRPC_Active.ico >nul 2>&1
copy MediaRPC_Inactive.ico dist\MediaRPC_Inactive.ico >nul 2>&1

echo.
echo Done! Run: dist\MediaRPC.exe
pause
