# GSM(Python運用)を最新化して再起動する。
#   git pull → 依存パッケージ更新 → GSM(GUI+サービス)を再起動
# デスクトップの「GSM を更新」ショートカットから実行する想定。
# exe を使わないので Smart App Control(未署名ブロック)の影響を受けない。

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Say($msg, $color = 'Cyan') { Write-Host $msg -ForegroundColor $color }

Say "=== GSM 更新 ==="
Write-Host "フォルダ: $PSScriptRoot`n"

# --- 未コミットの変更があれば確認する(勝手に上書きしない) ---
$dirty = git status --porcelain
if ($dirty) {
    Say "⚠ ローカルに未コミットの変更があります:" 'Yellow'
    $dirty | Select-Object -First 10 | ForEach-Object { Write-Host "   $_" }
    $ans = Read-Host "`nこのまま git pull を続けますか? (y/N)"
    if ($ans -ne 'y') { Write-Host "中止しました。"; Read-Host "Enterで閉じる"; exit }
}

# --- 1. コード更新 ---
Say "`n[1/3] git pull origin main ..."
git pull origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n❌ git pull に失敗しました。手動で確認してください。" -ForegroundColor Red
    Read-Host "Enterで閉じる"; exit 1
}

# --- 2. 依存パッケージ(requirements.txt が増えている場合に備えて) ---
Say "`n[2/3] 依存パッケージを確認 ..."
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
if (-not (Test-Path $py)) { $py = 'python' }
& $py -m pip install -q -r requirements.txt

# --- 3. GSM を再起動(GUI と サービス の両方) ---
Say "`n[3/3] GSM を再起動 ..."
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*main_app.py*' } |
    ForEach-Object {
        Write-Host "   停止: PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 1

$pyw = "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = 'pythonw' }
Start-Process -FilePath $pyw -ArgumentList 'main_app.py' -WorkingDirectory $PSScriptRoot

Write-Host "`n✅ 完了。最新版で GSM を起動しました。" -ForegroundColor Green
Write-Host "   現在のコミット: $(git rev-parse --short HEAD)"
Read-Host "`nEnterで閉じる"
