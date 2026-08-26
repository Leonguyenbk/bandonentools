@echo off
echo ==============================================
echo Dang build BandoNen_Tool.exe bang PyInstaller...
echo ==============================================
.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name "BandoNen_Tool" --collect-all rasterio --collect-all mercantile --copy-metadata boto3 app_gui.py
if exist config.local.json (
    copy /Y config.local.json dist\config.local.json
)
echo.
echo Build hoan tat! File thuc thi nam tai: dist\BandoNen_Tool.exe
pause
