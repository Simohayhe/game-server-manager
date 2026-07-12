#!/usr/bin/env bash
# ゲームサーバーマネージャー セットアップ (Linux / macOS)
#   bash setup.sh
set -e
cd "$(dirname "$0")"

echo "== ゲームサーバーマネージャー セットアップ =="

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3 が見つかりません。3.10 以降を入れてください。" >&2
  exit 1
fi
echo "Python: $($PY --version)"

echo "依存ライブラリを導入中 (pip install -r requirements.txt)…"
"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install -r requirements.txt

if [ ! -f config.yaml ]; then
  cp config.yaml.example config.yaml
  echo "config.yaml を作成しました。中身を自分の環境に書き換えてください。"
else
  echo "config.yaml は既に存在します(上書きしません)。"
fi

echo ""
echo "== 完了 =="
echo "1) config.yaml を編集(パスワード等)"
echo "2) 起動:  $PY main.py"
