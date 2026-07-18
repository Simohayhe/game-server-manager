# ゲームサーバーマネージャー セットアップ (Windows / PowerShell)
# 使い方:  右クリック → PowerShellで実行  または  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "== ゲームサーバーマネージャー セットアップ ==" -ForegroundColor Cyan

# 1) Python 確認
$py = $null
foreach ($c in @("py -3", "python", "python3")) {
    try { & cmd /c "$c --version" *> $null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
}
if (-not $py) {
    Write-Host "Python 3 が見つかりません。https://www.python.org/ から 3.10 以降を入れてください。" -ForegroundColor Red
    exit 1
}
Write-Host "Python: $py" -ForegroundColor Green

# 2) 依存ライブラリ
Write-Host "依存ライブラリを導入中 (pip install -r requirements.txt)…"
& cmd /c "$py -m pip install --upgrade pip" | Out-Null
& cmd /c "$py -m pip install -r requirements.txt"
if ($LASTEXITCODE -ne 0) { Write-Host "pip install に失敗しました。" -ForegroundColor Red; exit 1 }

# 3) config.yaml を用意
if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.yaml.example" "config.yaml"
    Write-Host "config.yaml を作成しました。中身を自分の環境に書き換えてください。" -ForegroundColor Yellow
} else {
    Write-Host "config.yaml は既に存在します(上書きしません)。" -ForegroundColor Green
}

Write-Host ""
Write-Host "== 完了 ==" -ForegroundColor Cyan
Write-Host "1) config.yaml を編集(パスワード等)"
Write-Host "2) 起動:  $py main_app.py    (ウィンドウを出さない常駐は pythonw main_app.py)"
