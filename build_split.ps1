# 新アーキ(サービス + 新GUI)を2つのexeにビルドする。
#   powershell -ExecutionPolicy Bypass -File build_split.ps1
# 生成物: dist\GSM-Service.exe(常駐サービス) / dist\GSM.exe(GUI)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = "C:\Users\master\AppData\Local\Programs\Python\Python312\python.exe"

Write-Host "== PyInstaller 導入確認 ==" -ForegroundColor Cyan
& $py -m pip install --upgrade pyinstaller *> $null

# 常駐サービス(コンソール付き=検証でログを見るため。本番はwindowedにしてよい)
Write-Host "== サービスをビルド中 ==" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --onefile --console --name GSM-Service `
    --collect-submodules paramiko --collect-submodules core --collect-submodules service `
    --add-data "provisioners;provisioners" main_service.py
if ($LASTEXITCODE -ne 0) { Write-Host "サービスのビルド失敗" -ForegroundColor Red; exit 1 }

# 新GUI(customtkinterのデータ同梱が必須)
Write-Host "== GUIをビルド中 ==" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name GSM `
    --collect-all customtkinter --collect-submodules gui --collect-submodules core `
    main_gsm.py
if ($LASTEXITCODE -ne 0) { Write-Host "GUIのビルド失敗" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "dist\GSM-Service.exe と dist\GSM.exe"
