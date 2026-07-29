$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$py = "C:\Users\master\AppData\Local\Programs\Python\Python312\python.exe"
& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name GameServerManager `
    --collect-all customtkinter `
    --collect-submodules paramiko --collect-submodules core --collect-submodules service --collect-submodules gui `
    --add-data "provisioners;provisioners" --add-data "web;web" main_app.py
if ($LASTEXITCODE -eq 0) { "OK" } else { "FAIL" }
