# game-server-manager 設計書

Hyper-V ホスト上の VM と、その中／ホスト上で動くゲームサーバー（Minecraft / ARK: Survival Ascended / Palworld）を
**1つのデスクトップアプリから管理**するツールの設計ドキュメント。

- 対象読者: このコードを引き継ぐ開発者
- 規模: 約 13,400 行 / core 37・service 13・gui 14 モジュール
- 言語/UI: Python 3.12 + customtkinter(tkinter)。追加ランタイム不要（標準ライブラリ + paramiko/PyYAML/ruamel.yaml/customtkinter）

---

## 1. 目的と前提

- **1画面で全部**: VM の起動/停止/複製/IP変更、ゲームサーバーの起動/停止/再起動/設定/バックアップ/更新/外部公開、定期再起動、監視、Discord通知。
- **このソフトは Hyper-V ホスト上で直接動かす**（`config.yaml: hyperv.mode: local`）。VM操作はローカル PowerShell。別マシンからの管理用に `mode: ssh` も実装済み。
- **VMのOSはLinux(Ubuntu)**。ゲームサーバーは systemd 化し、ホストから SSH(paramiko) で `systemctl` 操作。
- **ARK だけはホストのプロセスとして動く**（VM内だと EOS 参加不可の実地結論。詳細は各メモ参照）。ARKはVM/SSHとは別系統でホストのプロセス/RCONを直接制御。
- **core/ はUI非依存**。将来 Web版(FastAPI等)を同じ core/ の上に載せられる設計。

---

## 2. 全体アーキテクチャ（3層 + 常駐分離）

```mermaid
flowchart TB
    subgraph GUI["gui/ — customtkinter GUI（閉じても常駐は生きる）"]
        A[app_ctk<br/>タブUI/一覧/右クリック] --> CL[client.py<br/>HTTP JSONクライアント]
    end
    subgraph SVC["service/ — 常駐サービス（HTTP JSON API :8770）"]
        API[api/routes<br/>ルーティング] --> RUN[runner<br/>並列ジョブキュー/永続化]
        MON[monitor<br/>状態監視/通知/DNS監視]
        SCH[scheduler<br/>定期再起動/BK]
        REC[recovery<br/>クラッシュ自動復旧]
        PS[portsync_svc<br/>自動ポート開放]
        DYN[dynserve<br/>ARK動的設定HTTP配信]
        ST[state<br/>共有状態(スナップショット)]
    end
    subgraph CORE["core/ — UI非依存のドメインロジック"]
        direction LR
        TR[transport<br/>local PS / SSH]
        GS[gameserver/arkhost/palhost<br/>ゲーム制御]
        NET[hyperv/upnp/dnsreg/publish<br/>VM/ネットワーク]
        OPS[scheduler/notify/backup/settings<br/>運用]
    end
    CL -->|HTTP| API
    API --> CORE
    MON --> CORE
    SCH --> CORE
    CORE --> TR
    TR -->|PowerShell| HOST[(Hyper-Vホスト)]
    TR -->|SSH/RCON| VM[(Linux VM群)]
    ARKP[ARK: ホストのプロセス<br/>Start-Process/RCON] --- HOST
```

**要点**: GUI と 常駐サービスは**別プロセス**。`main_app.py` が自分自身を `--service` で detached 起動するので、
GUI を閉じても監視・予約・自動ポート開放・DNS監視は動き続ける。GUI はサービスの HTTP API を叩くだけの薄いクライアント。

---

## 3. レイヤー詳細

### 3.1 core/ — ドメインロジック（UI非依存・37モジュール）

| カテゴリ | モジュール | 役割 |
|---|---|---|
| 基盤 | `config` / `paths` / `transport` | config.yaml パース(dataclass) / exe同梱パス解決 / **local PowerShell と SSH を同一インターフェース(CommandResult)に抽象化** |
| ゲーム制御 | `rcon` / `gameserver` / `arkhost` / `palhost` | Source RCON自前実装(MC/ARK/Palworld共通) / VMのゲーム鯖(systemd) / **ARKホストプロセス制御(複数マップ・Port=で識別)** / Palworldホスト版 |
| ゲーム設定/更新 | `arkconfig` / `arkinstall` / `arkupdate` / `dynconfig` / `palconfig` / `palupdate` / `serverconfig` | ARK ini編集(コメント保持) / マップ別インストール分割 / SteamCMD更新 / **無停止動的設定(HTTP配信)** / Palworld設定・更新 / MC server.properties |
| VM/構築 | `hyperv` / `netscan` / `orchestration` / `provision` / `compat` | VMクローン/複製/個体化/IP変更 / IPスキャン・ARP / VM起動待ち→サービス起動 / Ubuntuに全自動構築(provisioners/*.yaml) / 動作環境判定 |
| ネットワーク | `dnsreg` / `upnp` / `pfm_upnp` / `publish` / `portsync` / `conntest` | phpIPAM/PowerDNS の A/PTR/SRV 自動登録 / UPnPポート開放(前作から取込) / WAN変動追随の外部公開 / **起動中だけ自動開放** / 外部視点のDNS/接続テスト |
| 運用 | `scheduler` / `notify` / `backup` / `settings` | 定期再起動/BK(schedules.json) / Discord通知(送信先/イベント/ゲーム別) / 圧縮・世代管理BK / config.yaml をコメント保持で書換え(ruamel) |
| Mod | `modmanager` / `moddeploy` / `onlinemods` | 手動mod管理(バージョン別) / InvSync自動配布 / Modrinth+CurseForge検索・依存解決・DL |
| その他 | `sqlshare` / `players` / `updatecheck` | サーバー間SQL共有(MariaDB) / プレイヤー数パース / GitHubリリース更新確認 |

**設計判断**: ゲーム固有の知識は core/ に**極力入れない**。config.yaml のプロファイル1ブロック（コマンド上書き・正規表現パターン）で新ゲームを足せる汎用設計。ゲーム固有が避けられない部分だけ `arkhost`/`palhost` 等に隔離。

### 3.2 service/ — 常駐サービス（13モジュール・HTTP JSON API :8770）

- `app`/`api`/`routes`: 標準ライブラリの HTTPServer + 自前ルーター。全操作を JSON API 化。
- `runner`: **並列ジョブキュー**（レーン単位で並行、重い更新が他を止めない）。`tasks.json` に永続化しGUI再起動でも履歴が残る。実行中→サービス再起動時は「中断」扱い。
- `context`: config読込 + ArkHost/GameServer インスタンス群を保持。`config_path`/`arkhosts`/`servers`。
- `state`: GUIに返す共有スナップショット（各サーバー/VMの状態・人数・バージョン等）。
- `monitor`: **ARK/サーバーを定期ポーリング**し状態を state に反映、状態遷移で Discord通知。起動完了(ARK=advertising)ラッチ、人数入退室、更新検知、**DNS健全性監視(_dns_loop)**、外部公開ステータス。
- `scheduler`: schedules.json を読み、時刻/曜日一致で定期再起動・BK発火（予告付き）。
- `recovery`: 意図的マーク無しの停止=クラッシュ→自動復旧（クールダウン付き）。
- `portsync_svc`: 稼働中サーバーのポートだけ UPnP 開放（所有権モデルで手動公開も引き継ぐ）。
- `dynserve`: ARK の動的設定を `http://127.0.0.1:PORT/dynamicconfig.ini` で text/plain 配信。
- `pubstat`/`history`: 外部公開ステータス照合 / 人数推移の記録。

### 3.3 gui/ — customtkinter GUI（14モジュール）

- `app_ctk`: 本体。タブUI（サーバー管理/ARK/予約/通知/設定/タスク…）、一覧Treeview、**操作は右クリックメニューに集約**。上部に常時リソースバー・更新通知。
- `client`: サービスの HTTP API を叩く薄いクライアント（`ApiError`/`ServiceUnavailable`）。
- `dashboard`/`widgets`/`dialogs`/`mod_dialog`/`notify_page`/`sched_page`/`settings_specs`: 各画面/部品。
- `firstrun`/`setupwizard`: 初回の動作環境チェック・config.yaml入力ウィザード。

---

## 4. ゲーム別の制御モデル

| ゲーム | 実行場所 | 起動/停止 | 状態/情報 | 設定ファイル |
|---|---|---|---|---|
| **Minecraft** | Linux VM | SSH `systemctl` (`gameserver.py`) | RCON list + ログ | server.properties(SSH経由) |
| **Palworld** | Linux VM | SSH `systemctl` | RCON ShowPlayers | PalWorldSettings.ini(1行OptionSettings) |
| **ARK ASA** | **ホストのプロセス** | `Start-Process -WindowStyle Hidden` + RCON saveworld/DoExit (`arkhost.py`) | プロセス有無(Port=で識別) + RCON ListPlayers + ログの `advertising for join` | GameUserSettings.ini / Game.ini(ホスト) |

- ARKは**マップ別に個別インストール**（`C:\ArkServers\<map>`）でマップ単位更新が可能。RCONポート/SessionNameは launch_args から取得するので ini は共有可能。
- 予告付き再起動・停止（15/10/5/1分）＋在席監視＋**チャット `no` で中止**（ARK=GetChat / MC・Palworld=ログの`[CHAT]`行）を3ゲーム共通で実装。

---

## 5. トランスポート抽象（core/transport.py）

`LocalPowerShell`(ホスト) と `SSHTransport`(VM) が同じ `run()`/`run_ps()` → **`CommandResult`(ok/stdout/stderr)** を返す。
呼び出し側はどちらか意識しない。`config.yaml: hyperv.mode` が local/ssh を切替。VM 個々の SSH は全VM共通のユーザー/パスワード。

---

## 6. 設定と状態ファイル

- **config.yaml**（唯一の設定・秘密含む・gitignore）: hyperv / network / dns / mysql / publish / backup / servers(MC/Palworld) / ark_hosts(ARK全マップ) / ark_steamcmd / curseforge。GUIから `settings.py`(ruamel)でコメント保持のまま書換え・検証・失敗時ロールバック。
- **状態json**（exe隣・gitignore）: `tasks.json`(ジョブ履歴) / `schedules.json`(予約) / `notify.json`(通知先) / `dynconfig.json`(動的設定) / `portsync.json` / `crashwatch.json` / `arkbehavior.json`。サービスがGUIからの変更を mtime で即反映。

---

## 7. 主要機能一覧

VM: クローン/複製/個体化/IP変更/メモリ・CPU変更/複数選択一括/安全停止(先にゲーム鯖停止)。
サーバー: 起動/停止/再起動(予告付き)/RCON/詳細設定(日本語)/バックアップ・復元(世代管理)/更新(SteamCMD)/外部公開(A+SRV+UPnP)/Mod管理(Modrinth+CF・依存解決)。
ARK固有: マップ別インストール・更新、無停止動的設定(色/倍率)、野生恐竜リスポーン、クラスタ、転送設定。
横断: 定期再起動サイクル(曜日指定)、クラッシュ自動復旧、Discord通知(送信先/イベント/ゲーム別)、自動ポート開放、外部公開WAN追随、**IPAM DNS監視→連続通知**、リソースバー、アプリ内更新通知。

---

## 8. 拡張のしかた

- **新ゲーム(VM/systemd型)**: config.yaml に servers プロファイル1ブロック（service名・RCON・players_command・正規表現パターン）を足す。core/ は基本触らない。
- **新プロビジョナ**: `provisioners/<game>.yaml`（bash テンプレ・`{{name}}` プレースホルダ）を1つ足すと構築ウィザードで使える。
- **新しい通知イベント**: `core/notify.py` の `DEFAULT_EVENTS`/`EVENT_LABELS` に足し、`monitor` 等から `self.notifier(event, text, game)` を呼ぶ。
- **新しい定期チェック**: `service/monitor.py` に throttled ループ（`_dns_loop` が参考）を足し `start()` にスレッド登録。

---

## 9. ビルド・配布

- 開発起動: `python main_app.py`（初回=環境チェック→ウィザード→サービス起動→GUI）。GUIのみ=`python main_gsm.py`、サービスのみ=`python main_service.py`。
- exe: `build_single.ps1`（PyInstaller `--onefile --windowed --collect-all customtkinter --add-data provisioners` main_app.py）。
- CI: `.github/workflows/build.yml`（tag push で exe ビルド→リリース添付）。**SignPath署名**は承認後に有効化（`SIGNPATH_SETUP.md` 参照）。
- 配布: GitHub Releases。非対応PCは軽量版(game-server-manager-lite)へ誘導。

---

## 10. 既知の制約・判断

- Windows の **Smart App Control(enforce)** が未署名exeを弾く → 本番は当面 python 運用。SignPath承認で解消予定。
- **ARKはホスト運用**（VMではEOS参加不可＋Proton経由は実プレイが重い、を実地確認）。
- 秘密情報は config.yaml と各種json（すべて gitignore）。公開リポに秘密を出さない運用。
