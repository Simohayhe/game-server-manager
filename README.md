# ゲームサーバーマネージャー

Hyper-V上のVMと、その中で動くゲームサーバー(Minecraft / ARK など)を
1つの画面から管理するツール。**Hyper-Vホスト上で直接動かす**(`hyperv.mode: local`)。

- **VM操作**: PowerShell(Hyper-Vコマンドレット)経由でVMの一覧・起動・シャットダウン
- **サーバー操作**: 各VM(Linux)にSSH接続し、systemd経由で起動・停止・再起動・ログ閲覧
- **ゲーム内情報**: RCON(Minecraft / Source RCON共通)でプレイヤー一覧など
- **拡張**: 新しいゲームは `config.yaml` にプロファイルを1ブロック追加するだけ

## 構成

```
main_app.py          エントリポイント
config.yaml      接続先とサーバープロファイル定義
core/            コアライブラリ(GUIと将来のWeb版で共通)
  transport.py     ローカルPowerShell / SSH実行
  hyperv.py        VM操作
  rcon.py          RCONクライアント(Minecraft/ARK共通)
  gameserver.py    サーバープロファイルと操作
  config.py        設定読み込み
gui/app.py       tkinter GUI
```

## 主な機能

- VM管理(一覧/起動/シャットダウン/強制停止)と、サーバーの起動/停止/再起動/ログ
- RCONコンソール、プレイヤー数・バージョン表示
- バックアップ/復元(zip・tar.gz、世代管理、mod鯖は mods/config も含む)
- 予約(定期再起動・定期バックアップ)、クラッシュ自動復旧、Discord通知
- タスク画面(全操作の成否と実行ロジックの可視化)、CPU/メモリ/ネットのリソースバー
- 新規サーバーの自動構築(テンプレVMをクローン→個体化→ゲーム構築→DNS登録)
- 外部公開(FQDN/UPnPポート開放/PowerDNS連携)、mod管理
- 対応ゲーム: Minecraft(Fabric/Forge) / ARK: Survival Ascended / Palworld ほか
  (`config.yaml` に1ブロック追加で拡張)

## 使い方

```
# Windows
powershell -ExecutionPolicy Bypass -File setup.ps1
# Linux / macOS
bash setup.sh
```

`setup` が依存導入と `config.yaml`(= `config.yaml.example` のコピー)の用意まで行います。
その後 `config.yaml` の `CHANGE_ME` とIPアドレスを自分の環境に書き換えて起動:

```
python main_app.py           # 常駐(ウィンドウ非表示)は pythonw main_app.py
```

## セットアップ手順

### 1. Hyper-Vホスト(Windows)側 = このソフトを動かすマシン

1. Python 3.10以上をインストールし、`pip install -r requirements.txt`。
2. 実行ユーザーが**管理者**または **Hyper-V Administrators** グループに入っていること
   (Get-VM / Start-VM の実行に必要)。
3. 別マシンから管理したくなった場合はOpenSSHサーバーを有効化し、
   `config.yaml` を `hyperv.mode: ssh` に切り替える(コード側は対応済み)。

### 2. ゲームサーバーVM(Ubuntu Server推奨)側

1. OpenSSHサーバーは通常インストール済み(`sudo apt install openssh-server`)。
2. VMのIPアドレスを固定する(ルーターのDHCP予約 or netplanで静的IP)。
3. ゲームサーバーをsystemdサービス化する。Minecraftの例
   (`/etc/systemd/system/minecraft.service`):
   ```ini
   [Unit]
   Description=Minecraft Server
   After=network.target

   [Service]
   User=minecraft
   WorkingDirectory=/opt/minecraft
   ExecStart=/usr/bin/java -Xmx4G -Xms1G -jar server.jar nogui
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```
   ```
   sudo systemctl daemon-reload
   sudo systemctl enable minecraft
   ```
4. **パスワードなしsudoの許可**(起動/停止/ログ取得に必要)。
   `sudo visudo -f /etc/sudoers.d/gamemanager` で以下を追加:
   ```
   ubuntu ALL=(ALL) NOPASSWD: /usr/bin/systemctl start minecraft, /usr/bin/systemctl stop minecraft, /usr/bin/systemctl restart minecraft, /usr/bin/journalctl
   ```
5. **RCONの有効化**。Minecraftは `server.properties`:
   ```
   enable-rcon=true
   rcon.port=25575
   rcon.password=好きなパスワード
   ```
   ARKは起動オプション `?RCONEnabled=True?RCONPort=27020` と
   `GameUserSettings.ini` の `ServerAdminPassword`。

### 3. 起動

ホスト上で `config.yaml` の `CHANGE_ME` とVMのIPアドレスを記入して `python main_app.py`。

## exe化(任意)

```
pyinstaller --onefile --windowed --name GameServerManager main_app.py
```

- `config.yaml` はexeと同じフォルダに置く
  (main_app.py は自身の場所から config.yaml を探す)。
- `--windowed` 時のコンソールなし環境は考慮済み
  (PowerShell呼び出しは CREATE_NO_WINDOW 指定、エラーはダイアログ表示)。

## 今後の拡張候補

- Web版ダッシュボード(coreをそのまま使ってFastAPI + ブラウザUI → スマホ対応)
- VMを使わない軽量版(SSHで届くLinuxホスト/Dockerを直接管理)

## ライセンス

MIT License(`LICENSE` 参照)。
