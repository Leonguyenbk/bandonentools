@echo off
echo =======================================================
echo Dang build BandoNen_Tool.exe (CustomTkinter + Icon Pro)...
echo =======================================================

.venv\Scripts\pyinstaller --noconfirm BandoNen_Tool.spec

if exist config.local.json (
    copy /Y config.local.json dist\config.local.json
)
if exist config.example.json (
    copy /Y config.example.json dist\config.example.json
)
if not exist dist\assets (
    mkdir dist\assets
)
copy /Y assets\* dist\assets\

echo.
echo =======================================================
echo Build hoan tat! File thuc thi: dist\BandoNen_Tool.exe
echo =======================================================
pause
