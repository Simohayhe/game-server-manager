# 接続専用GUI(GSM-Connect.exe)をビルドする。
# サービスを起動せず、既存/別PCのGSMサービスにGUIで繋ぐだけのexe。
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$py = "C:\Users\master\AppData\Local\Programs\Python\Python312\python.exe"
& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name GSM-Connect `
    --collect-all customtkinter `
    --collect-submodules paramiko --collect-submodules core --collect-submodules service --collect-submodules gui `
    --add-data "provisioners;provisioners" gsm_connect.py
if ($LASTEXITCODE -eq 0) { "OK -> dist\GSM-Connect.exe" } else { "FAIL" }
