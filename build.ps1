# ゲームサーバーマネージャー exe ビルド (PyInstaller)
#   powershell -ExecutionPolicy Bypass -File build.ps1
# 生成物: dist\GameServerManager.exe (単一ファイル)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Python を探す
$py = $null
foreach ($c in @("py -3", "python", "python3")) {
    try { & cmd /c "$c --version" *> $null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
}
if (-not $py) { Write-Host "Python 3 が見つかりません" -ForegroundColor Red; exit 1 }

Write-Host "== 依存 + PyInstaller を導入 ==" -ForegroundColor Cyan
& cmd /c "$py -m pip install --upgrade pip"        | Out-Null
& cmd /c "$py -m pip install -r requirements.txt"  | Out-Null
& cmd /c "$py -m pip install --upgrade pyinstaller" | Out-Null

Write-Host "== ビルド中(数分かかります) ==" -ForegroundColor Cyan
# provisioners/ は実行時に読む同梱データなので --add-data で入れる
& cmd /c "$py -m PyInstaller --noconfirm --clean --onefile --windowed --name GameServerManager --add-data `"provisioners;provisioners`" --collect-submodules paramiko main.py"
if ($LASTEXITCODE -ne 0) { Write-Host "ビルド失敗" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "dist\GameServerManager.exe をダブルクリックで起動できます。"
Write-Host "初回は 動作環境チェック → セットアップ入力 が出て、exeの隣に config.yaml が作られます。"
