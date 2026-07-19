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

## インストール / 配布

- **通常のユーザーは `GameServerManager-Setup.exe`(インストーラ)を実行**する。
  `C:\Program Files\GameServerManager\` に本体を入れ、**デスクトップ＋スタートメニュー**に
  ショートカットを作成し、アンインストーラも登録する(インストール時に UAC)。
- ユーザーデータ(`config.yaml`・各種`.json`・初回マーカー)は本体とは別に
  **`%LOCALAPPDATA%\GameServerManager\`** に保存される(書込可・exeの場所に依存しない)。
  → **更新で本体を入れ替えても設定はそのまま**残る。旧版(exe隣にデータを置く版)からは
  初回起動時に自動で引っ越す。
- インストーラは Inno Setup (`installer.iss`) でビルドする:
  `ISCC.exe /DMyAppVersion=X.Y.Z installer.iss` → `installer_out\GameServerManager-Setup.exe`。
  CI(タグ push)が exe とインストーラの両方をリリースに添付する。
- 単体 exe だけ作るなら:
  `pyinstaller --onefile --windowed --name GameServerManager main_app.py`。

## アップデート

- 起動時に GitHub Releases を確認し、新バージョンがあれば上部バーに
  `🔔 新バージョン`(クリック)を表示する。
- クリックで**アプリ内更新**: 最新の `Setup.exe` をDL → 実行(UAC昇格して GSM を停止 →
  上書きインストール → 再起動)。設定は `%LOCALAPPDATA%` にあるため引き継がれる。
  インストーラが無いリリースでは exe を直接入れ替える方式にフォールバック。
- source実行時は自己更新できないため、クリックでリリースページを開く。

## Discord ボット(任意)

Discord からゲームサーバーを起動/停止/状態確認できる常駐ボット(`discordbot.py`)。
アウトバウンド接続のみで動くため、ルーターのポート開放は不要。

```
python -u discordbot.py    # 常時起動しておく(-u はログを即時出力するため)
```

- **設定**: `config.yaml` の `discord:` セクション(token/guild_id/admin_role_id/
  allowed_servers/log_channel_id)。詳細は `config.yaml.example` 参照。
  bot は Discord Developer Portal で各自作成し、token を設定する。
- **コマンド**: `/gs list`(一覧) `/gs status` `/gs start` `/gs stop` `/gs restart`
  (サーバー名はオートコンプリート)。
- **権限(2層)**:
  - **ボット管理者** = `admin_role_id` のロール(未設定なら管理者権限のあるユーザー)。
    全サーバーを操作でき、`/permission` で他人の権限も管理できる。
  - **個別権限** = ボット管理者が `/permission add <ユーザー> <権限>` で配る。
    権限(スコープ)は3段階: `all`(全部) / ゲーム単位(`minecraft`/`ark`/`palworld`) /
    個別サーバー(`minecraft2` 等)。付与/剥奪は **Discord のコマンドだけで完結**し、
    `permissions.json` に保存され bot 再起動後も保持、変更は即反映。
  - 管理コマンド: `/permission add` `/permission remove` `/permission list`
    (いずれもボット管理者のみ)。
- **安全設計**: `allowed_servers` で操作可能なサーバーを限定でき(空=全許可)、
  **稼働中サーバーの停止/再起動は確認ボタンを必須**(プレイヤー切断の誤操作防止)。
  操作・権限変更は stdout と(設定時)`log_channel_id` に「誰が何をしたか」を記録する。

## 今後の拡張候補

- Web版ダッシュボード(coreをそのまま使ってFastAPI + ブラウザUI → スマホ対応)
- VMを使わない軽量版(SSHで届くLinuxホスト/Dockerを直接管理)

## ライセンス

MIT License(`LICENSE` 参照)。
