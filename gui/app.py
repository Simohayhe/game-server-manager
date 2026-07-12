"""tkinter GUI。

SSH/PowerShell/RCON/MySQLはすべて時間のかかるI/Oなので、ワーカースレッドで実行し、
結果はキュー経由でメインスレッドに渡してUIを更新する
(tkinterのウィジェット操作はメインスレッド限定のため)。
"""
from __future__ import annotations

import queue
import socket
import threading
import time
import tkinter as tk
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from core import provision
from core.arkhost import ArkHost
from core.config import AppConfig, load_config
from core.gameserver import GameServer
from core.hyperv import HyperVManager
from core import (arkconfig, arkupdate, backup, conntest, dnsreg, dynconfig,
                  moddeploy, modmanager, netscan, notify, onlinemods, palconfig,
                  palupdate, portsync, publish, scheduler, serverconfig, settings,
                  updatecheck, upnp)
from core.orchestration import (change_vm_ip, individualize_clone,
                                start_server_with_vm)
from core.sqlshare import SqlShareManager
from core.transport import LocalPowerShell, SSHTransport

REFRESH_INTERVAL_MS = 20_000
PUBLISH_CHECK_MS = 600_000  # 外部公開ヘルスチェック間隔(10分)
SCHED_TICK_MS = 20_000      # 再起動予約の発火チェック間隔
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
SCHEDULES_PATH = CONFIG_PATH.parent / "schedules.json"  # 再起動予約の永続化
DYNSTATE_PATH = CONFIG_PATH.parent / "dynconfig.json"   # dynamic configの状態
DYNFILE_PATH = CONFIG_PATH.parent / "dynamicconfig.ini"  # 配信するdynamic config本体
PORTSYNC_PATH = CONFIG_PATH.parent / "portsync.json"    # 自動ポート開放の状態
PORTSYNC_TICK_MS = 30_000   # 自動ポート開放の照合間隔
NOTIFY_PATH = CONFIG_PATH.parent / "notify.json"        # Discord通知の設定
CRASH_PATH = CONFIG_PATH.parent / "crashwatch.json"     # クラッシュ自動復旧の状態
ARKBEHAVIOR_PATH = CONFIG_PATH.parent / "arkbehavior.json"  # ARK再起動時の挙動(恐竜リスポーン等)
RESMON_MS = 3000    # リソース表示バーの更新間隔
PUBSTAT_MS = 60000  # 外部公開ステータスの照合間隔(UPnP+DNS。重いので長め)
APP_VERSION = "1.0.0"                          # このアプリのバージョン(リリースtagと比較)
GITHUB_REPO = "Simohayhe/game-server-manager"  # アップデート確認先
# ホストのCPU%/メモリ(使用|合計 MB)/ネット(受信|送信 Bytes/sec)を1行で取得
RESMON_PS = (
    "$c=(Get-CimInstance Win32_Processor|Measure-Object -Property LoadPercentage -Average).Average;"
    "$o=Get-CimInstance Win32_OperatingSystem;"
    "$mt=[math]::Round($o.TotalVisibleMemorySize/1MB,1);"
    "$mu=[math]::Round(($o.TotalVisibleMemorySize-$o.FreePhysicalMemory)/1MB,1);"
    "$n=Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface|"
    "Where-Object{$_.Name -notmatch 'Loopback|isatap|Pseudo'};"
    "$rx=($n|Measure-Object -Property BytesReceivedPersec -Sum).Sum;"
    "$tx=($n|Measure-Object -Property BytesSentPersec -Sum).Sum;"
    '"$c|$mu|$mt|$rx|$tx"'
)

# 詳細設定(Minecraft server.properties)の整形対象。(キー, 種別, 選択肢)
# 種別: bool=true/false、choice=(値,日本語)のドロップダウン、str/int=入力欄。
# ここに無いキーは「その他」欄で生値編集(未知キーもそのまま保持される)。
MC_PROPERTIES_CURATED = [
    ("motd", "str", None),
    ("max-players", "int", None),
    ("difficulty", "choice", [("peaceful", "ピースフル(平和)"), ("easy", "イージー"),
                              ("normal", "ノーマル"), ("hard", "ハード")]),
    ("gamemode", "choice", [("survival", "サバイバル"), ("creative", "クリエイティブ"),
                            ("adventure", "アドベンチャー"), ("spectator", "スペクテイター")]),
    ("pvp", "bool", None),
    ("hardcore", "bool", None),
    ("white-list", "bool", None),
    ("enforce-whitelist", "bool", None),
    ("online-mode", "bool", None),
    ("allow-nether", "bool", None),
    ("allow-flight", "bool", None),
    ("enable-command-block", "bool", None),
    ("force-gamemode", "bool", None),
    ("spawn-protection", "int", None),
    ("view-distance", "int", None),
    ("simulation-distance", "int", None),
    ("level-name", "str", None),
    ("level-seed", "str", None),
    ("level-type", "str", None),
]

# server.properties の各キーの日本語ラベル(詳細設定ダイアログの表示用)。
# ここに無いキーは英語キー名のまま表示する。
PROP_LABELS_JA = {
    "motd": "サーバー説明(MOTD)",
    "max-players": "最大プレイヤー数",
    "difficulty": "難易度",
    "gamemode": "ゲームモード",
    "pvp": "PvP(対人戦)",
    "hardcore": "ハードコア",
    "white-list": "ホワイトリスト",
    "enforce-whitelist": "ホワイトリストを強制",
    "online-mode": "オンライン認証",
    "allow-nether": "ネザーを許可",
    "allow-flight": "飛行を許可",
    "enable-command-block": "コマンドブロックを有効化",
    "force-gamemode": "ゲームモードを強制",
    "spawn-protection": "スポーン保護の範囲",
    "view-distance": "描画距離(チャンク)",
    "simulation-distance": "シミュレーション距離",
    "level-name": "ワールド名",
    "level-seed": "シード値",
    "level-type": "地形タイプ",
    "server-port": "サーバーポート",
    "server-ip": "サーバーIP(通常は空)",
    "spawn-monsters": "モンスターの湧き",
    "spawn-animals": "動物の湧き",
    "spawn-npcs": "村人(NPC)の湧き",
    "generate-structures": "構造物を生成",
    "max-world-size": "ワールドの最大サイズ",
    "player-idle-timeout": "放置キックまでの時間(分)",
    "pause-when-empty-seconds": "無人時に一時停止(秒)",
    "op-permission-level": "OPの権限レベル",
    "function-permission-level": "function権限レベル",
    "enable-rcon": "RCONを有効化",
    "rcon.port": "RCONポート",
    "rcon.password": "RCONパスワード",
    "enable-query": "クエリを有効化",
    "query.port": "クエリポート",
    "enable-status": "サーバー状態を公開",
    "hide-online-players": "オンライン人数を隠す",
    "broadcast-console-to-ops": "コンソールをOPに通知",
    "broadcast-rcon-to-ops": "RCON結果をOPに通知",
    "resource-pack": "リソースパックURL",
    "resource-pack-sha1": "リソースパックSHA1",
    "require-resource-pack": "リソースパックを必須",
    "resource-pack-prompt": "リソースパックの案内文",
    "enforce-secure-profile": "セキュアプロフィールを強制",
    "prevent-proxy-connections": "プロキシ接続を拒否",
    "rate-limit": "パケットレート制限",
    "network-compression-threshold": "通信圧縮のしきい値",
    "use-native-transport": "ネイティブ通信を使用",
    "sync-chunk-writes": "チャンク書き込みを同期",
    "max-tick-time": "最大tick時間(ms)",
    "entity-broadcast-range-percentage": "エンティティ表示範囲(%)",
    "max-chained-neighbor-updates": "連鎖近隣更新の上限",
    "text-filtering-config": "テキストフィルタ設定",
    "initial-enabled-packs": "初期有効データパック",
    "initial-disabled-packs": "初期無効データパック",
    "accepts-transfers": "サーバー転送を受け入れ",
    "bug-report-link": "バグ報告リンク",
    "log-ips": "IPをログに記録",
    "enable-jmx-monitoring": "JMX監視を有効化",
    "region-file-compression": "リージョンファイル圧縮",
    "chat-spam-threshold-seconds": "チャットスパムしきい値(秒)",
    "command-spam-threshold-seconds": "コマンドスパムしきい値(秒)",
    "generator-settings": "地形ジェネレーター設定",
    "white-list-blank": "ホワイトリスト(空)",
    "enable-code-of-conduct": "行動規範を有効化",
    "enforce-secure-profiles": "セキュアプロフィールを強制",
}

# ARK詳細設定エディタの整形フィールド。
#   (fileキー, セクション, iniキー, 種別, 日本語ラベル, 既定値)
#   fileキー: "gus"=GameUserSettings.ini / "game"=Game.ini。種別: bool / float / int
#   既定値: 未設定(空欄)のときにグレーで表示するゲーム既定。倍率系は基本1.0。
#   全マップ共有(config_dir共有)。ここに無いキーは触らずファイル内で保持される。
_ARK_GM = "/Script/ShooterGame.ShooterGameMode"
_SS = "ServerSettings"
_GSESS = "/Script/Engine.GameSession"
ARK_SETTINGS_TABS = [
    ("基本・倍率", [
        ("gus", _SS, "XPMultiplier", "float", "経験値(XP)倍率", "1.0"),
        ("gus", _SS, "TamingSpeedMultiplier", "float", "テイム速度倍率(大=速い)", "1.0"),
        ("gus", _SS, "HarvestAmountMultiplier", "float", "採取量倍率", "1.0"),
        ("gus", _SS, "HarvestHealthMultiplier", "float", "資源の耐久倍率(大=長持ち)", "1.0"),
        ("gus", _SS, "ResourcesRespawnPeriodMultiplier", "float", "資源リポップ間隔倍率(小=早い)", "1.0"),
        ("gus", _SS, "ItemStackSizeMultiplier", "float", "アイテムスタック倍率", "1.0"),
        ("gus", _SS, "DinoCountMultiplier", "float", "野生恐竜の数倍率", "1.0"),
        ("gus", _SS, "OverrideOfficialDifficulty", "float", "難易度上書き(5.0=野生最大Lv150)", "5.0"),
        ("gus", _SS, "DifficultyOffset", "float", "難易度オフセット(0〜1)", "1.0"),
        ("gus", _SS, "MaxTamedDinos", "int", "サーバー全体のテイム上限数", "5000"),
        ("game", _ARK_GM, "MaxNumberOfPlayersInTribe", "int", "トライブ最大人数(0=無制限)", "0"),
    ]),
    ("時間・環境", [
        ("gus", _SS, "DayCycleSpeedScale", "float", "1日の進行速度倍率", "1.0"),
        ("gus", _SS, "DayTimeSpeedScale", "float", "昼の長さ倍率", "1.0"),
        ("gus", _SS, "NightTimeSpeedScale", "float", "夜の長さ倍率", "1.0"),
        ("gus", _SS, "GlobalSpoilingTimeMultiplier", "float", "食料の腐敗時間倍率(大=腐りにくい)", "1.0"),
        ("gus", _SS, "GlobalItemDecompositionTimeMultiplier", "float", "ドロップ品の消滅時間倍率", "1.0"),
        ("gus", _SS, "GlobalCorpseDecompositionTimeMultiplier", "float", "死体の消滅時間倍率", "1.0"),
        ("game", _ARK_GM, "CropGrowthSpeedMultiplier", "float", "作物の成長速度倍率", "1.0"),
        ("game", _ARK_GM, "CropDecaySpeedMultiplier", "float", "作物の枯れ速度倍率", "1.0"),
        ("game", _ARK_GM, "PoopIntervalMultiplier", "float", "排泄間隔倍率", "1.0"),
        ("game", _ARK_GM, "LayEggIntervalMultiplier", "float", "採卵間隔倍率", "1.0"),
        ("game", _ARK_GM, "HairGrowthSpeedMultiplier", "float", "毛/ヒゲの伸び速度倍率", "1.0"),
    ]),
    ("プレイヤー", [
        ("gus", _SS, "PlayerCharacterFoodDrainMultiplier", "float", "空腹の減り倍率", "1.0"),
        ("gus", _SS, "PlayerCharacterWaterDrainMultiplier", "float", "水分の減り倍率", "1.0"),
        ("gus", _SS, "PlayerCharacterStaminaDrainMultiplier", "float", "スタミナの減り倍率", "1.0"),
        ("gus", _SS, "PlayerCharacterHealthRecoveryMultiplier", "float", "体力回復倍率", "1.0"),
        ("gus", _SS, "PlayerDamageMultiplier", "float", "プレイヤー与ダメ倍率", "1.0"),
        ("gus", _SS, "PlayerResistanceMultiplier", "float", "プレイヤー被ダメ倍率(小=硬い)", "1.0"),
        ("gus", _SS, "OxygenSwimSpeedStatMultiplier", "float", "酸素→泳速の倍率", "1.0"),
        ("game", _ARK_GM, "OverrideMaxExperiencePointsPlayer", "int", "プレイヤー最大経験値(空=既定)", ""),
    ]),
    ("恐竜", [
        ("gus", _SS, "DinoCharacterFoodDrainMultiplier", "float", "恐竜の空腹の減り倍率", "1.0"),
        ("gus", _SS, "DinoCharacterStaminaDrainMultiplier", "float", "恐竜のスタミナ減り倍率", "1.0"),
        ("gus", _SS, "DinoCharacterHealthRecoveryMultiplier", "float", "恐竜の体力回復倍率", "1.0"),
        ("gus", _SS, "DinoDamageMultiplier", "float", "野生恐竜の与ダメ倍率", "1.0"),
        ("gus", _SS, "DinoResistanceMultiplier", "float", "野生恐竜の被ダメ倍率(小=硬い)", "1.0"),
        ("gus", _SS, "TamedDinoDamageMultiplier", "float", "テイム恐竜の与ダメ倍率", "1.0"),
        ("gus", _SS, "TamedDinoResistanceMultiplier", "float", "テイム恐竜の被ダメ倍率(小=硬い)", "1.0"),
        ("gus", _SS, "AllowFlyerCarryPvE", "bool", "飛行生物で他生物を掴む(PvE)", "False"),
        ("gus", _SS, "AllowRaidDinoFeeding", "bool", "タイタノ等の餌付けテイムを許可", "False"),
        ("gus", _SS, "MaxTamedDinos_SoftTameLimit", "int", "テイム軟上限(超過分は消去対象)", "5000"),
    ]),
    ("繁殖", [
        ("game", _ARK_GM, "MatingIntervalMultiplier", "float", "交配クールダウン倍率(小=再交配が早い)", "1.0"),
        ("game", _ARK_GM, "MatingSpeedMultiplier", "float", "発情までの速度倍率(大=速い)", "1.0"),
        ("game", _ARK_GM, "EggHatchSpeedMultiplier", "float", "卵の孵化速度倍率(大=速い)", "1.0"),
        ("game", _ARK_GM, "BabyMatureSpeedMultiplier", "float", "赤ちゃん成長速度倍率(大=速い)", "1.0"),
        ("game", _ARK_GM, "BabyFoodConsumptionSpeedMultiplier", "float", "赤ちゃんの餌消費倍率(小=省エネ)", "1.0"),
        ("game", _ARK_GM, "BabyCuddleIntervalMultiplier", "float", "刷り込み間隔倍率(小=刷り込み頻度↓)", "1.0"),
        ("game", _ARK_GM, "BabyCuddleGracePeriodMultiplier", "float", "刷り込み猶予時間倍率", "1.0"),
        ("game", _ARK_GM, "BabyImprintAmountMultiplier", "float", "刷り込み1回あたりの量倍率", "1.0"),
        ("game", _ARK_GM, "BabyImprintingStatScaleMultiplier", "float", "刷り込みによるステ上昇倍率", "1.0"),
        ("gus", _SS, "AllowAnyoneBabyImprintCuddle", "bool", "刷り込みを誰でも行える", "False"),
    ]),
    ("構造物", [
        ("gus", _SS, "StructureDamageMultiplier", "float", "建築物の与ダメ倍率", "1.0"),
        ("gus", _SS, "StructureResistanceMultiplier", "float", "建築物の被ダメ倍率(小=頑丈)", "1.0"),
        ("gus", _SS, "PvEStructureDecayPeriodMultiplier", "float", "PvE建築の崩壊猶予倍率(大=長持ち)", "1.0"),
        ("gus", _SS, "DisableStructureDecayPvE", "bool", "PvE建築の自動崩壊を無効化", "False"),
        ("gus", _SS, "TheMaxStructuresInRange", "int", "一定範囲内の最大建築数", "10500"),
        ("gus", _SS, "AlwaysAllowStructurePickup", "bool", "建築物をいつでも回収可能", "False"),
        ("gus", _SS, "StructurePickupTimeAfterPlacement", "float", "設置後に回収できる秒数", "30"),
        ("gus", _SS, "StructurePickupHoldDuration", "float", "回収の長押し秒数", "0.5"),
        ("gus", _SS, "PerPlatformMaxStructuresMultiplier", "float", "プラットフォーム上の建築上限倍率", "1.0"),
        ("gus", _SS, "AllowCaveBuildingPvE", "bool", "洞窟内の建築を許可(PvE)", "False"),
    ]),
    ("ルール・PvP/PvE", [
        ("gus", _SS, "serverPVE", "bool", "PvEモード(ON=PvPなし・恐竜同士戦わない)", "False"),
        ("gus", _SS, "ServerHardcore", "bool", "ハードコア(死亡でLv1に)", "False"),
        ("gus", _SS, "ServerCrosshair", "bool", "照準(クロスヘア)表示", "False"),
        ("gus", _SS, "ServerForceNoHUD", "bool", "HUDを強制非表示", "False"),
        ("gus", _SS, "ShowMapPlayerLocation", "bool", "マップに自分の位置を表示", "False"),
        ("gus", _SS, "ShowFloatingDamageText", "bool", "ダメージ数値を表示", "False"),
        ("gus", _SS, "AllowThirdPersonPlayer", "bool", "三人称視点を許可", "True"),
        ("gus", _SS, "EnablePVPGamma", "bool", "PvPでガンマ調整を許可", "False"),
        ("gus", _SS, "DisableFriendlyFire", "bool", "同トライブへの誤射を無効化", "False"),
        ("gus", _SS, "GlobalVoiceChat", "bool", "全体ボイスチャット", "False"),
        ("gus", _SS, "ProximityChat", "bool", "近接チャットのみ", "False"),
        ("gus", _SS, "AllowHitMarkers", "bool", "ヒットマーカー表示", "True"),
        ("gus", _SS, "KickIdlePlayersPeriod", "int", "放置キックまでの秒数", "3600"),
        ("gus", _SS, "TribeNameChangeCooldown", "int", "トライブ名変更のクールダウン(分)", "15"),
        ("gus", _SS, "AutoSavePeriodMinutes", "float", "オートセーブ間隔(分)", "15"),
        ("gus", _GSESS, "MaxPlayers", "int", "最大プレイヤー数", "70"),
    ]),
]

# Palworld詳細設定エディタ。(iniキー, 種別, 日本語ラベル, 既定値, choices)
#   種別: float / int / bool / str / choice。全て OptionSettings=(...) の中身。
_PAL_DEATH = [("None", "なし(ドロップしない)"), ("Item", "アイテムのみ"),
              ("ItemAndEquipment", "アイテム＋装備"), ("All", "全部(パル含む)")]
PAL_SETTINGS_TABS = [
    ("倍率(QoL)", [
        ("ExpRate", "float", "経験値倍率", "1.0", None),
        ("PalCaptureRate", "float", "パル捕獲率(大=捕まえやすい)", "1.0", None),
        ("PalSpawnNumRate", "float", "パルの出現数倍率", "1.0", None),
        ("CollectionDropRate", "float", "採取量倍率", "1.0", None),
        ("CollectionObjectRespawnSpeedRate", "float", "採取物の再出現速度(小=速い)", "1.0", None),
        ("EnemyDropItemRate", "float", "敵ドロップ倍率", "1.0", None),
        ("WorkSpeedRate", "float", "パルの作業速度倍率", "1.0", None),
        ("PalEggDefaultHatchingTime", "float", "卵の孵化時間(時間・短=速い)", "72.0", None),
    ]),
    ("生活・時間", [
        ("PlayerStomachDecreaceRate", "float", "プレイヤー空腹の減り(小=減りにくい)", "1.0", None),
        ("PalStomachDecreaceRate", "float", "パルの空腹の減り", "1.0", None),
        ("PlayerStaminaDecreaceRate", "float", "スタミナの減り", "1.0", None),
        ("PlayerAutoHPRegeneRate", "float", "自動HP回復倍率", "1.0", None),
        ("DayTimeSpeedRate", "float", "昼の進行速度", "1.0", None),
        ("NightTimeSpeedRate", "float", "夜の進行速度", "1.0", None),
        ("BuildObjectDamageRate", "float", "建築物の被ダメ倍率", "1.0", None),
        ("BuildObjectDeteriorationDamageRate", "float", "建築物の劣化速度(0=劣化なし)", "1.0", None),
    ]),
    ("難易度・ルール", [
        ("PalDamageRateAttack", "float", "パルの与ダメ倍率", "1.0", None),
        ("PalDamageRateDefense", "float", "パルの被ダメ倍率(小=硬い)", "1.0", None),
        ("PlayerDamageRateAttack", "float", "プレイヤーの与ダメ倍率", "1.0", None),
        ("PlayerDamageRateDefense", "float", "プレイヤーの被ダメ倍率(小=硬い)", "1.0", None),
        ("DeathPenalty", "choice", "死亡ペナルティ", "Item", _PAL_DEATH),
        ("bEnablePlayerToPlayerDamage", "bool", "PvP(プレイヤー間ダメージ)", "False", None),
        ("bEnableFriendlyFire", "bool", "フレンドリーファイア", "False", None),
        ("bHardcore", "bool", "ハードコア(死亡でキャラロスト)", "False", None),
        ("bEnableFastTravel", "bool", "ファストトラベル許可", "True", None),
    ]),
    ("拠点・ギルド", [
        ("BaseCampWorkerMaxNum", "int", "拠点のパル最大数", "15", None),
        ("BaseCampMaxNum", "int", "拠点の最大数", "128", None),
        ("GuildPlayerMaxNum", "int", "ギルド最大人数", "20", None),
        ("BaseCampMaxNumInGuild", "int", "ギルドの拠点最大数", "4", None),
        ("DropItemMaxNum", "int", "地面ドロップ最大数", "3000", None),
        ("bAllowGlobalPalboxExport", "bool", "グローバルパルボックス書出許可", "True", None),
        ("bAllowGlobalPalboxImport", "bool", "グローバルパルボックス取込許可", "False", None),
    ]),
    ("サーバー", [
        ("ServerName", "str", "サーバー名", "", None),
        ("ServerDescription", "str", "サーバー説明", "", None),
        ("ServerPlayerMaxNum", "int", "最大プレイヤー数", "32", None),
        ("ServerPassword", "str", "参加パスワード(空=なし)", "", None),
        ("bIsPvP", "bool", "PvPモード", "False", None),
        ("bEnableInvaderEnemy", "bool", "襲撃イベントを有効", "True", None),
        ("SupplyDropSpan", "int", "サプライドロップ間隔(分)", "180", None),
    ]),
]

# ---- 配色パレット(モダンな明るいテーマ) ----
PAL = {
    "bg": "#eceff5",         # ウィンドウ背景
    "surface": "#ffffff",    # カード/フレーム
    "surface_alt": "#f5f7fb",
    "border": "#d7dce6",
    "text": "#25303c",
    "muted": "#7c8698",
    "accent": "#4f7cf0",     # プライマリ
    "accent_hover": "#3d69df",
    "on_accent": "#ffffff",
    "ok": "#1f9d57",
    "off": "#8a94a3",
    "error": "#e0524a",
    "busy": "#e08a1e",
    "heading": "#333d4d",
    "row_alt": "#f5f7fb",
    "sel": "#dbe6ff",
}

# ゲーム種別 → セクション見出し
GAME_SECTIONS = {
    "minecraft": "🟩  Minecraft",
    "ark": "🦖  ARK",
    "palworld": "🐑  Palworld",
}

# Treeview行の色分けタグ(パレット連動)
TAG_COLORS = {
    "ok": PAL["ok"],
    "off": PAL["off"],
    "error": PAL["error"],
    "busy": PAL["busy"],
}

# VMの状態 → 表示テキストとタグ
VM_STATE_VIEW = {
    "Running": ("Running", "ok"),
    "Off": ("Off", "off"),
    "Starting": ("起動中…", "busy"),
    "Stopping": ("停止中…", "busy"),
    "Saved": ("Saved", "off"),
    "Paused": ("Paused", "busy"),
}


def server_status_view(status: str) -> tuple[str, str]:
    """systemctl is-active等の出力 → (表示テキスト, 色タグ)。"""
    if status == "active":
        return "Active", "ok"
    if status in ("inactive", "dead"):
        return "Stop", "off"
    if status == "activating":
        return "起動中…", "busy"
    if status == "deactivating":
        return "停止中…", "busy"
    return f"Error ({status})", "error"


class _Task:
    """1つの操作(起動/停止/設定変更/バックアップ等)の実行記録。

    タスク画面に一覧表示し、クリックで詳細(実行ステップのログ・結果・エラー)を見る。
    失敗時の切り分けを楽にするための可視化用。ワーカーが log を追記し、UIが表示する。
    """
    _counter = 0

    def __init__(self, title: str, category: str = "操作"):
        _Task._counter += 1
        self.id = _Task._counter
        self.title = title
        self.category = category
        self.status = "running"        # running / success / failed
        self.started = datetime.now()
        self.ended: datetime | None = None
        self.log: list[str] = []       # 実行ステップ(進捗メッセージ)
        self.error: str | None = None  # 失敗時の例外文字列

    def add(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {text}")

    @property
    def status_label(self) -> str:
        return {"running": "⏳ 実行中",
                "success": "✅ 成功",
                "failed": "❌ 失敗"}.get(self.status, self.status)

    @property
    def elapsed(self) -> str:
        end = self.ended or datetime.now()
        secs = (end - self.started).total_seconds()
        return f"{secs:.1f}秒"


class App(tk.Tk):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.title("ゲームサーバーマネージャー")
        # 内容ぴったりの固定サイズで中央に開く(必要サイズ ~1033x736)
        w, h = 1060, 760
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(1040, 620)
        self.config_data = config
        self._apply_style()

        if config.hyperv.mode == "local":
            runner = LocalPowerShell()
        else:
            runner = SSHTransport(
                host=config.hyperv.host,
                user=config.hyperv.user,
                port=config.hyperv.port,
                key=config.hyperv.key,
                password=config.hyperv.password,
            )
        self.hyperv = HyperVManager(runner)
        self.servers = {p.name: GameServer(p) for p in config.servers}
        self.sqlshare = SqlShareManager(config.mysql) if config.mysql else None
        self.modlib = modmanager.ModLibrary(base_dir=CONFIG_PATH.parent)
        self.arkhosts = [ArkHost(c, self.hyperv._runner) for c in config.ark_hosts]
        self.backupcfg = config.backup or backup.BackupConfig()
        # 定期再起動の予約(schedules.jsonから復元=アプリ再起動でも維持)
        self.schedules = scheduler.load_jobs(SCHEDULES_PATH)
        self._sched_fired: set = set()   # (job_id, "HH:MM", "YYYY-MM-DD") 二重発火抑止
        # ARK dynamic config(無停止で倍率を変更)。状態を復元してサーバー起動。
        self.dynstate = dynconfig.load_state(DYNSTATE_PATH)
        self.dynserver = dynconfig.DynConfigServer(DYNFILE_PATH, self.dynstate.port)
        self._dyn_apply_initial()
        # 自動ポート開放(起動中だけ開ける)。状態はportsync.jsonで永続化。
        self.autoport_enabled = portsync.load_enabled(PORTSYNC_PATH)
        self._server_running: dict[str, bool] = {}   # MCサーバー名 -> 稼働中
        self._ark_running: dict[int, bool] = {}       # ARK index -> 稼働中
        self._portsync_gw = None
        self._portsync_gw_ts = 0.0
        # Discord通知 / クラッシュ自動復旧
        self.notifycfg = notify.load(NOTIFY_PATH)
        import json as _json
        try:
            self.crash_recovery = bool(_json.loads(
                CRASH_PATH.read_text(encoding="utf-8")).get("enabled", False)) \
                if CRASH_PATH.exists() else False
        except Exception:
            self.crash_recovery = False
        self._op_stop: dict[str, float] = {}      # GSMが停止操作した時刻(クラッシュ誤検知防止)
        self._op_restart: dict[str, float] = {}   # GSMが再起動操作した時刻
        self._crash_cooldown: dict[str, float] = {}  # 自動復旧の連続実行防止
        # ARK再起動時の挙動(詳細設定でON/OFF): ONなら全ARK再起動で野生恐竜をリスポーン
        import json as _json2
        try:
            self.ark_respawn_on_restart = bool(_json2.loads(
                ARKBEHAVIOR_PATH.read_text(encoding="utf-8")).get("respawn_on_restart", False)) \
                if ARKBEHAVIOR_PATH.exists() else False
        except Exception:
            self.ark_respawn_on_restart = False
        # ARKサーバー更新(SteamCMD)。steamcmdはconfig優先、無ければ既存インストールから導出。
        self.ark_steamcmd = config.ark_steamcmd or ""
        if not self.ark_steamcmd and self.arkhosts:
            try:
                sc = self.arkhosts[0].cfg.install_root.parent / "steamcmd" / "steamcmd.exe"
                if sc.exists():
                    self.ark_steamcmd = str(sc)
            except Exception:
                self.ark_steamcmd = ""
        self.arkupdate_enabled = bool(self.ark_steamcmd and Path(self.ark_steamcmd).exists())
        self._ark_update_latest = None            # 最新build
        self._ark_update_builds = {}              # install_root(str) -> installed build

        # サーバー名 → 検出済みバージョン(セッション中キャッシュ)
        self._versions: dict[str, str] = {}
        self._version_fetching: set[str] = set()
        # サーバー名 → FQDNの解決先IP(起動時に裏で解決してキャッシュ)
        self._fqdn_ips: dict[str, str] = {}
        # MAC → IP(ARPテーブル。VM一覧のIP列解決用)
        self._arp_cache: dict[str, str] = {}
        # VM名 → 状態(サーバー依存の判定用)
        self._vm_states: dict[str, str] = {}
        self._busy_count = 0

        # タスク記録(タスク画面): 起動/停止/設定変更等の操作を記録・可視化する
        self._tasks: list[_Task] = []      # 新しい順に前へ差し込む
        self._active_task: _Task | None = None  # ワーカーで今実行中のタスク(進捗の紐付け先)
        self._task_selected_id: int | None = None  # タスク画面で選択中のタスクID

        # ワーカースレッド: ジョブを直列に実行し、完了コールバックをUIキューへ流す
        self._jobs: queue.Queue = queue.Queue()
        self._ui_queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

        self._build_ui()
        # リソース表示バーの監視スレッド(ホストのCPU/メモリ/ネットを定期取得)
        self._resmon_stop = False
        if config.hyperv.mode == "local":
            self._resmon_runner = LocalPowerShell()
        else:
            self._resmon_runner = SSHTransport(
                host=config.hyperv.host, user=config.hyperv.user,
                port=config.hyperv.port, key=config.hyperv.key,
                password=config.hyperv.password)
        self._resmon_thread = threading.Thread(target=self._resmon_loop, daemon=True)
        self._resmon_thread.start()
        # 外部公開ステータスの監視スレッド(UPnP転送 + DNS→WAN を照合)
        self._pubstat_stop = False
        self._pubstat_thread = threading.Thread(target=self._pubstat_loop, daemon=True)
        self._pubstat_thread.start()
        # 起動時にGitHubの新バージョンを1回だけ確認(バックグラウンド)
        threading.Thread(target=self._update_check_once, daemon=True).start()
        self._resolve_fqdns()
        # VM範囲を一度スイープしてARPテーブルを温める(IP列のMAC逆引き用)
        net = config.network
        self._submit(lambda: netscan.scan_used_octets(net.prefix, *net.vm_range),
                     lambda _r, _e: self.refresh_all())
        self.after(100, self._poll_ui_queue)
        self.after(200, self.refresh_all)
        if self.sqlshare is not None:
            self.after(400, self._sql_refresh)
        self.after(REFRESH_INTERVAL_MS, self._auto_refresh)
        # 外部公開ヘルスチェック(WAN IP変動の検知・通知)
        self._publish_last_action = False
        if self.config_data.publish is not None:
            self.after(3000, self._publish_check)
            self.after(PUBLISH_CHECK_MS, self._publish_auto_check)
        self.after(SCHED_TICK_MS, self._sched_tick)   # 再起動予約の発火チェック
        self.after(8000, self._portsync_tick)         # 自動ポート開放の照合
        if self.arkhosts and self.arkupdate_enabled:
            self.after(10000, self._ark_update_auto_check)  # ARK更新チェック
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 見た目(スタイル) ----------

    def _apply_style(self) -> None:
        self.configure(bg=PAL["bg"])
        base_font = ("Segoe UI", 10)
        self.option_add("*Font", base_font)
        # tk(非ttk)ウィジェット既定色
        self.option_add("*Menu.background", PAL["surface"])
        self.option_add("*Menu.foreground", PAL["text"])
        self.option_add("*Menu.activeBackground", PAL["accent"])
        self.option_add("*Menu.activeForeground", PAL["on_accent"])
        self.option_add("*Menu.relief", "flat")
        self.option_add("*Toplevel.background", PAL["bg"])

        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=PAL["bg"], foreground=PAL["text"],
                     fieldbackground=PAL["surface"], font=base_font, borderwidth=0)
        st.configure("TFrame", background=PAL["bg"])
        st.configure("TLabel", background=PAL["bg"], foreground=PAL["text"])
        st.configure("TLabelframe", background=PAL["bg"], borderwidth=1,
                     relief="solid", bordercolor=PAL["border"])
        st.configure("TLabelframe.Label", background=PAL["bg"],
                     foreground=PAL["accent"], font=("Segoe UI", 10, "bold"))

        # ボタン: フラットで角丸風、ホバーでアクセント寄り
        st.configure("TButton", background=PAL["surface"], foreground=PAL["text"],
                     bordercolor=PAL["border"], relief="flat", padding=(8, 4))
        st.map("TButton",
               background=[("pressed", PAL["accent_hover"]), ("active", PAL["sel"])],
               foreground=[("pressed", PAL["on_accent"])],
               bordercolor=[("active", PAL["accent"])])

        # Entry / Combobox
        for cls in ("TEntry", "TCombobox"):
            st.configure(cls, fieldbackground=PAL["surface"], background=PAL["surface"],
                         bordercolor=PAL["border"], foreground=PAL["text"], padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", PAL["surface"])])

        # Notebook(タブ)
        st.configure("TNotebook", background=PAL["bg"], borderwidth=0, tabmargins=(6, 6, 6, 0))
        st.configure("TNotebook.Tab", background=PAL["surface_alt"], foreground=PAL["muted"],
                     padding=(18, 8), font=("Segoe UI", 10, "bold"), borderwidth=0)
        st.map("TNotebook.Tab",
               background=[("selected", PAL["surface"])],
               foreground=[("selected", PAL["accent"])],
               expand=[("selected", (0, 0, 0, 0))])

        # Treeview(一覧)
        st.configure("Treeview", background=PAL["surface"], fieldbackground=PAL["surface"],
                     foreground=PAL["text"], rowheight=27, borderwidth=1,
                     bordercolor=PAL["border"])
        st.configure("Treeview.Heading", background=PAL["heading"], foreground="#ffffff",
                     font=("Segoe UI", 9, "bold"), relief="flat", padding=6)
        st.map("Treeview.Heading", background=[("active", PAL["accent"])])
        st.map("Treeview", background=[("selected", PAL["accent"])],
               foreground=[("selected", PAL["on_accent"])])

        # プログレスバー / スクロールバー / ステータス
        st.configure("TProgressbar", background=PAL["accent"], troughcolor=PAL["surface_alt"],
                     bordercolor=PAL["border"])
        st.configure("Vertical.TScrollbar", background=PAL["surface_alt"],
                     troughcolor=PAL["bg"], bordercolor=PAL["bg"], arrowcolor=PAL["muted"])
        st.configure("Status.TLabel", background=PAL["heading"], foreground="#e8ecf3",
                     padding=(10, 5))
        st.configure("Section.Treeview", rowheight=27)

    # ---------- ワーカー ----------

    def _worker_loop(self) -> None:
        while True:
            job, on_done, task = self._jobs.get()
            self._active_task = task     # 実行中に流れる進捗をこのタスクへ紐付ける
            try:
                result = job()
                self._ui_queue.put((on_done, result, None))
            except Exception as exc:
                traceback.print_exc()
                self._ui_queue.put((on_done, None, exc))
            finally:
                self._active_task = None

    def _submit(self, job, on_done) -> None:
        """job(引数なし呼び出し)をワーカーで実行し、on_done(result, error)をUIスレッドで呼ぶ。"""
        self._jobs.put((job, on_done, None))

    def _task_submit(self, title, job, on_done=None, category="操作", busy=True):
        """タスク画面に記録しながら操作を実行する。

        job内の self._progress_from_worker(...) はワーカーが実行中タスクへ自動で追記する。
        完了時にタスクを成功/失敗で確定し、元の on_done(result, error) を呼ぶ。
        """
        task = _Task(title, category)
        task.add("開始")
        self._tasks.insert(0, task)
        self._task_tree_add(task)
        if busy:
            self._begin_busy(title + " …")

        def wrapped(result, error):
            task.ended = datetime.now()
            if error is not None:
                task.status = "failed"
                task.error = f"{type(error).__name__}: {error}"
                task.add(f"失敗: {task.error}")
            else:
                task.status = "success"
                task.add(f"完了({task.elapsed})")
            self._task_tree_update(task)
            if self._task_selected_id == task.id:
                self._task_show_detail(task)
            if busy:
                self._end_busy()
            if on_done is not None:
                on_done(result, error)

        self._jobs.put((job, wrapped, task))
        return task

    def _progress_from_worker(self, text: str) -> None:
        """ワーカースレッドから途中経過をステータスバー(+実行中タスク)へ流す。"""
        task = self._active_task          # 呼び出し時点の実行中タスクを捕捉
        self._ui_queue.put(
            (lambda _r, _e, t=text, tk_=task: self._on_worker_progress(t, tk_),
             None, None))

    def _on_worker_progress(self, text: str, task: "_Task | None") -> None:
        self._set_status(text)
        if task is not None:
            task.add(text)
            if self._task_selected_id == task.id:
                self._task_show_detail(task)

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                on_done, result, error = self._ui_queue.get_nowait()
                on_done(result, error)
        except queue.Empty:
            pass
        self.after(100, self._poll_ui_queue)

    # ---------- UI構築 ----------

    # ---------- リソース表示バー(全タブ共通・ホストのCPU/メモリ/ネット) ----------

    def _build_resource_bar(self) -> None:
        bar = tk.Frame(self, bg=PAL["heading"])
        bar.pack(fill=tk.X, side=tk.TOP)
        self.res_cpu_var = tk.StringVar(value="🖥 CPU --")
        self.res_mem_var = tk.StringVar(value="🧠 メモリ --")
        self.res_net_var = tk.StringVar(value="🌐 ネット --")
        common = dict(bg=PAL["heading"], fg="#ffffff", font=("Segoe UI", 10))
        self.res_cpu_lbl = tk.Label(bar, textvariable=self.res_cpu_var, **common)
        self.res_cpu_lbl.pack(side=tk.LEFT, padx=(10, 16), pady=3)
        self.res_mem_lbl = tk.Label(bar, textvariable=self.res_mem_var, **common)
        self.res_mem_lbl.pack(side=tk.LEFT, padx=(0, 16))
        tk.Label(bar, textvariable=self.res_net_var, **common).pack(side=tk.LEFT)
        tk.Label(bar, text="ホスト", bg=PAL["heading"], fg="#c8d0dc",
                 font=("Segoe UI", 9)).pack(side=tk.RIGHT, padx=10)
        # アップデート通知(新版がある時だけ点灯・クリックでリリースページ)
        self._update_url = f"https://github.com/{GITHUB_REPO}/releases"
        self.update_var = tk.StringVar(value="")
        self.update_lbl = tk.Label(bar, textvariable=self.update_var,
                                   bg=PAL["heading"], fg="#ffd166",
                                   font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.update_lbl.pack(side=tk.RIGHT, padx=6)
        self.update_lbl.bind("<Button-1>", lambda _e: self._open_update_url())

    @staticmethod
    def _parse_resmon(out: str):
        text = (out or "").strip()
        if not text:
            return None
        line = text.splitlines()[-1]
        parts = line.split("|")
        if len(parts) != 5:
            return None
        try:
            return {"cpu": float(parts[0] or 0), "mu": float(parts[1]),
                    "mt": float(parts[2]), "rx": float(parts[3] or 0),
                    "tx": float(parts[4] or 0)}
        except ValueError:
            return None

    def _resmon_loop(self) -> None:
        while not self._resmon_stop:
            try:
                r = self._resmon_runner.run_ps(RESMON_PS, timeout=15)
                data = self._parse_resmon(r.stdout)
            except Exception:
                data = None
            try:
                self._ui_queue.put(
                    (lambda _r, _e, d=data: self._resmon_update(d), None, None))
            except Exception:
                pass
            time.sleep(RESMON_MS / 1000)

    def _resmon_update(self, d) -> None:
        if not hasattr(self, "res_cpu_var"):
            return
        if d is None:
            self.res_cpu_var.set("🖥 CPU --")
            return
        cpu = d["cpu"]
        mu, mt = d["mu"], d["mt"]              # 既にGB単位
        mempct = round(mu / mt * 100) if mt else 0

        def net(bps):                          # 小さい時はKB/s、大きい時はMB/s
            return f"{bps / 1e6:.1f}MB/s" if bps >= 1e6 else f"{bps / 1e3:.0f}KB/s"

        self.res_cpu_var.set(f"🖥 CPU {round(cpu)}%")
        self.res_mem_var.set(f"🧠 メモリ {mu:.1f} / {mt:.1f} GB ({mempct}%)")
        self.res_net_var.set(f"🌐 ↓ {net(d['rx'])}  ↑ {net(d['tx'])}")
        cpu_col = ("#ff6b6b" if cpu >= 90 else "#ffd166" if cpu >= 70 else "#ffffff")
        mem_col = ("#ff6b6b" if mempct >= 90 else "#ffd166" if mempct >= 78 else "#ffffff")
        self.res_cpu_lbl.configure(fg=cpu_col)
        self.res_mem_lbl.configure(fg=mem_col)

    # ---- 外部公開ステータス(UPnP転送 + DNS→WAN の照合) ----
    def _pubstat_loop(self) -> None:
        time.sleep(6)                       # 起動直後の初期化と競合させない
        while not self._pubstat_stop:
            try:
                results = self._compute_pubstat()
            except Exception:
                results = {}
            if results:
                self._ui_queue.put(
                    (lambda _r, _e, d=results: self._pubstat_apply(d), None, None))
            for _ in range(int(PUBSTAT_MS / 500)):   # 停止フラグを見つつ分割スリープ
                if self._pubstat_stop:
                    break
                time.sleep(0.5)

    def _compute_pubstat(self) -> dict:
        """各サーバーの外部公開状態を返す(ワーカースレッド。tkinterに触れないこと)。

        判定材料: ①ルーターのUPnPポート転送が そのサーバー宛に存在するか
                  ②自FQDNのA解決が現WAN IPを指すか。両方揃えば「公開中」。
        """
        targets = [(n, s.profile) for n, s in self.servers.items()
                   if getattr(s.profile, "external_port", None)]
        arks = list(enumerate(self.arkhosts))
        if not targets and not arks:
            return {}
        gw = getattr(self, "_pubstat_gw", None)
        try:
            if gw is None:
                prefer = (self.config_data.network.gateway
                          if self.config_data.network else None)
                gw = upnp.find_gateway(prefer_host=prefer)
                self._pubstat_gw = gw
            mappings = gw.client.list_port_mappings()
            wan = gw.external_ip
        except Exception:
            self._pubstat_gw = None                  # 次回に再探索
            return {"servers": {n: "―" for n, _p in targets},   # UPnP不可=判定不能
                    "ark": {str(i): "―" for i, _ah in arks}}
        existing = {(str(m.get("external_port")), (m.get("protocol") or "").upper()): m
                    for m in mappings}
        resolver = self.config_data.dns.host if self.config_data.dns else None
        # --- VM上のサーバー(MC/Palworld): 転送 + DNS→WAN ---
        srv = {}
        for n, p in targets:
            proto = "UDP" if p.game == "palworld" else "TCP"
            m = existing.get((str(p.external_port), proto))
            forwarded = bool(m and m.get("internal_client") == p.address)
            dns_wan = False
            dns_checked = False
            fqdn = getattr(p, "fqdn", None)
            if resolver and fqdn and wan:
                dns_checked = True
                try:
                    dns_wan = wan in conntest.dns_query(resolver, fqdn, 1)  # A=1
                except Exception:
                    dns_checked = False        # 照会失敗=未確認(転送だけで判定)
            if forwarded and (dns_wan or not dns_checked):
                srv[n] = "🌐 公開中"            # 転送あり＋(DNS→WAN or DNS未確認)
            elif forwarded or dns_wan:
                srv[n] = "🟡 要確認"            # 片方だけ(例: 転送のみ / DNSのみ)
            else:
                srv[n] = "🔒 非公開"
        # --- ARK(ホストで動く): ゲーム/クエリの両UDPをホストIPへ転送しているか ---
        ark = {}
        if arks:
            try:
                host_ip = upnp.local_ip_toward(
                    self.config_data.network.gateway
                    if self.config_data.network else "192.168.11.1")
            except Exception:
                host_ip = None
            for i, ah in arks:
                gp = getattr(ah.cfg, "game_port", None)
                qp = getattr(ah.cfg, "query_port", None)
                gm = existing.get((str(gp), "UDP")) if gp else None
                qm = existing.get((str(qp), "UDP")) if qp else None
                g_ok = bool(gm and gm.get("internal_client") == host_ip)
                q_ok = bool(qm and qm.get("internal_client") == host_ip)
                if g_ok and q_ok:
                    ark[str(i)] = "🌐 公開中"    # ゲーム+クエリ両方 転送あり
                elif g_ok or q_ok:
                    ark[str(i)] = "🟡 要確認"    # 片方だけ
                else:
                    ark[str(i)] = "🔒 非公開"
        return {"servers": srv, "ark": ark}

    def _pubstat_apply(self, results: dict) -> None:
        if not results:
            return
        for name, text in results.get("servers", {}).items():
            if self.sv_tree.exists(name):
                try:
                    self.sv_tree.set(name, "public", text)
                except Exception:
                    pass
        if hasattr(self, "ark_tree"):
            for idx, text in results.get("ark", {}).items():
                if self.ark_tree.exists(idx):
                    try:
                        self.ark_tree.set(idx, "public", text)
                    except Exception:
                        pass

    # ---- アプリ内アップデート通知(GitHub Releases) ----
    def _update_check_once(self) -> None:
        """起動時に1回、GitHub Releasesで新版を確認(ワーカースレッド)。"""
        try:
            result = updatecheck.check_latest(GITHUB_REPO, APP_VERSION)
        except Exception:
            return
        self._ui_queue.put(
            (lambda _r, _e, d=result: self._update_check_apply(d), None, None))

    def _update_check_apply(self, result: dict) -> None:
        if not result or not result.get("update_available"):
            return
        latest = result.get("latest")
        self._update_url = result.get("url") or self._update_url
        if hasattr(self, "update_var"):
            self.update_var.set(f"🔔 新バージョン {latest}(クリック)")
        self._append_log(f"🔔 新しいバージョン {latest} が公開されています"
                         f"(現在 v{APP_VERSION}): {self._update_url}")
        self._set_status(f"新バージョン {latest} が利用可能です")

    def _open_update_url(self) -> None:
        import webbrowser
        try:
            webbrowser.open(self._update_url)
        except Exception:
            pass

    def _build_ui(self) -> None:
        self._build_resource_bar()          # 全タブ共通の上部リソースバー
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        manage_tab = ttk.Frame(notebook)
        sql_tab = ttk.Frame(notebook)
        mod_tab = ttk.Frame(notebook)
        sched_tab = ttk.Frame(notebook)
        task_tab = ttk.Frame(notebook)
        settings_tab = ttk.Frame(notebook)
        notebook.add(manage_tab, text=" サーバー管理 ")
        notebook.add(sql_tab, text=" SQL共有 ")
        notebook.add(mod_tab, text=" Mod管理 ")
        if self.arkhosts:
            ark_tab = ttk.Frame(notebook)
            notebook.add(ark_tab, text=" 🦖 ARK ")
        notebook.add(sched_tab, text=" ⏰ 再起動予約 ")
        notebook.add(task_tab, text=" 📋 タスク ")
        notebook.add(settings_tab, text=" 設定 ")

        self._build_manage_tab(manage_tab)
        self._build_sql_tab(sql_tab)
        self._build_mod_tab(mod_tab)
        if self.arkhosts:
            self._build_ark_tab(ark_tab)
        self._build_sched_tab(sched_tab)
        self._build_task_tab(task_tab)
        self._build_settings_tab(settings_tab)

        # --- ステータスバー(+進捗インジケーター) ---
        status_bar = ttk.Frame(self)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_var = tk.StringVar(value="準備完了")
        ttk.Label(status_bar, textvariable=self.status_var, anchor=tk.W,
                  style="Status.TLabel")\
            .pack(fill=tk.X, side=tk.LEFT, expand=True)
        self.progress = ttk.Progressbar(status_bar, mode="indeterminate", length=140)
        # 操作実行中のみ表示する(_begin_busy / _end_busy)

    def _build_manage_tab(self, parent: ttk.Frame) -> None:
        paned = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # --- VMセクション ---
        vm_frame = ttk.LabelFrame(paned, text="Hyper-V 仮想マシン")
        paned.add(vm_frame, weight=1)

        self.vm_tree = ttk.Treeview(
            vm_frame, columns=("state", "ip", "cpu", "mem", "uptime"),
            show="tree headings", height=5, selectmode="extended")
        self.vm_tree.heading("#0", text="VM名")
        self.vm_tree.heading("state", text="状態")
        self.vm_tree.heading("ip", text="IP")
        self.vm_tree.heading("cpu", text="CPU%")
        self.vm_tree.heading("mem", text="メモリ(MB)")
        self.vm_tree.heading("uptime", text="稼働時間")
        self.vm_tree.column("#0", width=200)
        for col, w in (("state", 100), ("ip", 80), ("cpu", 60),
                       ("mem", 100), ("uptime", 100)):
            self.vm_tree.column(col, width=w, anchor=tk.CENTER)
        self.vm_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 0))

        ttk.Label(
            vm_frame, foreground=PAL["muted"],
            text="↳ 一覧を右クリックで操作(VM起動 / シャットダウン / 強制停止 / VM設定 / IP変更 / 更新)"
        ).pack(anchor=tk.W, padx=8, pady=(2, 6))

        # --- ゲームサーバーセクション ---
        sv_frame = ttk.LabelFrame(paned, text="ゲームサーバー")
        paned.add(sv_frame, weight=2)

        self.sv_tree = ttk.Treeview(
            sv_frame,
            columns=("vm", "status", "public", "address", "port", "version", "players"),
            show="tree headings", height=5)
        self.sv_tree.heading("#0", text="サーバー")
        self.sv_tree.heading("vm", text="VM")
        self.sv_tree.heading("status", text="状態")
        self.sv_tree.heading("public", text="外部公開")
        self.sv_tree.heading("address", text="アドレス")
        self.sv_tree.heading("port", text="ポート")
        self.sv_tree.heading("version", text="バージョン")
        self.sv_tree.heading("players", text="プレイヤー数")
        self.sv_tree.column("#0", width=170)
        self.sv_tree.column("vm", width=100, anchor=tk.CENTER)
        self.sv_tree.column("status", width=90, anchor=tk.CENTER)
        self.sv_tree.column("public", width=90, anchor=tk.CENTER)
        self.sv_tree.column("address", width=170, anchor=tk.CENTER)
        self.sv_tree.column("port", width=60, anchor=tk.CENTER)
        self.sv_tree.column("version", width=80, anchor=tk.CENTER)
        self.sv_tree.column("players", width=180)
        self.sv_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 0))

        ttk.Label(
            sv_frame, foreground=PAL["muted"],
            text="↳ 一覧を右クリックで操作(起動 / 停止 / 再起動 / プレイヤー / ログ / 情報 / "
                 "詳細設定 / OP / 外部公開 / 接続テスト / 新規構築 / 更新)"
        ).pack(anchor=tk.W, padx=8, pady=(2, 0))

        # --- RCONコンソール ---
        rcon_bar = ttk.Frame(sv_frame)
        rcon_bar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(rcon_bar, text="RCON >").pack(side=tk.LEFT)
        self.rcon_entry = ttk.Entry(rcon_bar)
        self.rcon_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.rcon_entry.bind("<Return>", lambda _e: self._rcon_send())
        ttk.Button(rcon_bar, text="送信", command=self._rcon_send).pack(side=tk.LEFT)

        # 行の色分け
        for tree in (self.vm_tree, self.sv_tree):
            for tag, color in TAG_COLORS.items():
                tree.tag_configure(tag, foreground=color)

        # --- 右クリックメニュー ---
        self.vm_menu = tk.Menu(self, tearoff=0)
        self.vm_menu.add_command(label="▶ VM起動", command=self._vm_start)
        self.vm_menu.add_command(label="⏹ シャットダウン", command=self._vm_stop)
        self.vm_menu.add_command(label="⚡ 強制停止", command=self._vm_force_stop)
        self.vm_menu.add_command(label="🔧 VM設定", command=self._vm_settings)
        self.vm_menu.add_command(label="🌐 IP変更", command=self._vm_change_ip)
        self.vm_menu.add_command(label="📋 VMを複製(ゲーム構築なし)", command=self._vm_duplicate)
        self.vm_menu.add_separator()
        self.vm_menu.add_command(label="🔄 更新", command=self.refresh_all)

        self.sv_menu = tk.Menu(self, tearoff=0)
        self.sv_menu.add_command(label="✏ 名前を変更", command=self._sv_rename)
        self.sv_menu.add_command(label="📋 アドレスをコピー", command=self._sv_copy_address)
        self.sv_menu.add_separator()
        self.sv_menu.add_command(label="▶ 起動", command=lambda: self._sv_action("start"))
        self.sv_menu.add_command(label="⏹ 停止", command=lambda: self._sv_action("stop"))
        self.sv_menu.add_command(label="🔁 再起動", command=lambda: self._sv_action("restart"))
        self.sv_menu.add_separator()
        self.sv_menu.add_command(label="👥 プレイヤー確認", command=self._sv_players)
        self.sv_menu.add_command(label="📄 ログ表示", command=self._sv_log)
        self.sv_menu.add_command(label="ℹ サーバー情報", command=self._sv_info)
        self.sv_menu.add_command(label="⚙ 詳細設定", command=self._sv_server_config)
        self.sv_menu.add_command(label="💾 バックアップ/復元", command=self._sv_backup_open)
        self.sv_menu.add_command(label="🧩 Mod管理", command=self._sv_mod_manager)
        quick_menu = tk.Menu(self.sv_menu, tearoff=0)
        for label, cmd in [("💾 ワールド保存(save-all)", "save-all"),
                           ("☀ 時間を昼に(time set day)", "time set day"),
                           ("🌙 時間を夜に(time set night)", "time set night"),
                           ("🌤 天候を晴れに(weather clear)", "weather clear"),
                           ("💬 全体メッセージ(say)", "__say__")]:
            quick_menu.add_command(label=label, command=lambda c=cmd: self._sv_quick(c))
        self.sv_menu.add_cascade(label="⚡ クイックコマンド", menu=quick_menu)
        self.sv_menu.add_command(label="👑 OP管理", command=self._sv_op)
        self.sv_menu.add_command(label="🌍 外部公開(UPnP)", command=self._sv_publish)
        self.sv_menu.add_command(label="🚫 公開停止", command=self._sv_unpublish)
        self.sv_menu.add_command(label="🔌 接続テスト(外部視点)", command=self._sv_conntest)
        self.sv_menu.add_command(label="⬆ サーバー更新(Palworld)", command=self._sv_update)
        self.sv_menu.add_separator()
        self.sv_menu.add_command(label="⚙ 新規サーバー構築", command=self._sv_provision)
        self.sv_menu.add_command(label="🔄 更新", command=self.refresh_all)

        self.vm_tree.bind("<Button-3>", lambda e: self._popup_menu(e, self.vm_tree, self.vm_menu))
        self.sv_tree.bind("<Button-3>", lambda e: self._popup_menu(e, self.sv_tree, self.sv_menu))
        # サーバー名(#0列)ダブルクリックで名前変更、アドレス列ダブルクリックでコピー
        self.sv_tree.bind("<Double-1>", self._sv_tree_dblclick)

        # --- ログ表示エリア ---
        log_frame = ttk.LabelFrame(paned, text="ログ / 出力")
        paned.add(log_frame, weight=2)
        self.log_text = tk.Text(log_frame, wrap=tk.NONE, height=8, state=tk.DISABLED,
                                font=("Consolas", 9), bg="#20262f", fg="#d7dce6",
                                insertbackground="#d7dce6", relief=tk.FLAT,
                                borderwidth=0, padx=8, pady=6)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=(6, 0), pady=6)

        # ゲーム別セクション(親ノード)+ サーバー(子ノード)
        self.sv_tree.tag_configure("section", background=PAL["surface_alt"],
                                   foreground=PAL["heading"],
                                   font=("Segoe UI", 10, "bold"))
        self._build_server_rows()

    def _build_server_rows(self) -> None:
        """サーバー一覧をゲーム種別ごとのセクションに分けて構築する。"""
        by_game: dict[str, list[str]] = {}
        for name, gs in self.servers.items():
            by_game.setdefault(gs.profile.game, []).append(name)
        # セクション順: 既知(minecraft, ark…)を先に、その他を後に
        order = list(GAME_SECTIONS.keys())
        games = sorted(by_game, key=lambda g: (order.index(g) if g in order else 99, g))
        for game in games:
            sec_id = f"__sec__{game}"
            label = GAME_SECTIONS.get(game, f"🎮  {game}")
            self.sv_tree.insert("", tk.END, iid=sec_id, text=label, open=True,
                                tags=("section",), values=("", "", "", "", "", "", ""))
            for name in by_game[game]:
                profile = self.servers[name].profile
                addr, port = self._server_addr_port(profile)
                pub = "…" if getattr(profile, "external_port", None) else "―"
                self.sv_tree.insert(sec_id, tk.END, iid=name, text=profile.display_name,
                                    values=(profile.vm or "-", "…", pub, addr, port, "?", ""))

    def _build_sql_tab(self, parent: ttk.Frame) -> None:
        if self.sqlshare is None:
            ttk.Label(parent, text="config.yaml に mysql 設定がないため、SQL共有機能は無効です。",
                      padding=20).pack()
            return

        top = ttk.LabelFrame(parent, text="データ共有グループ(グループ=共有データベース)")
        top.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))

        self.sql_tree = ttk.Treeview(
            top, columns=("db", "members"), show="tree headings", height=8)
        self.sql_tree.heading("#0", text="グループ")
        self.sql_tree.heading("db", text="データベース")
        self.sql_tree.heading("members", text="参加サーバー")
        self.sql_tree.column("#0", width=160)
        self.sql_tree.column("db", width=180, anchor=tk.CENTER)
        self.sql_tree.column("members", width=480)
        self.sql_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 0))

        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(btns, text="➕ グループ作成", command=self._sql_create_group).pack(side=tk.LEFT)
        ttk.Button(btns, text="🗑 グループ削除", command=self._sql_delete_group).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="➕ サーバー追加", command=self._sql_add_server).pack(side=tk.LEFT)
        ttk.Button(btns, text="➖ サーバー除外", command=self._sql_remove_server).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="🔑 接続情報", command=self._sql_show_info).pack(side=tk.LEFT)
        ttk.Button(btns, text="🔄 更新", command=self._sql_refresh).pack(side=tk.RIGHT)

        info_frame = ttk.LabelFrame(parent, text="接続情報 / 出力")
        info_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.sql_text = tk.Text(info_frame, wrap=tk.NONE, height=10, state=tk.DISABLED,
                                font=("Consolas", 9), bg="#20262f", fg="#d7dce6",
                                insertbackground="#d7dce6", relief=tk.FLAT,
                                borderwidth=0, padx=8, pady=6)
        self.sql_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    # ---------- Mod管理タブ ----------

    def _build_mod_tab(self, parent: ttk.Frame) -> None:
        mc_servers = [s for s in self.servers.values()
                      if s.profile.game == "minecraft"]
        if not mc_servers:
            ttk.Label(parent, text="Minecraftサーバーがありません。",
                      padding=20).pack()
            return

        top = ttk.Frame(parent)
        top.pack(fill=tk.X, padx=8, pady=(8, 0))
        ttk.Label(top, text="対象サーバー:").pack(side=tk.LEFT)
        self._mod_server_names = {s.profile.display_name: s.profile.name
                                  for s in mc_servers}
        self.mod_server_var = tk.StringVar()
        self.mod_server_combo = ttk.Combobox(
            top, textvariable=self.mod_server_var, state="readonly",
            values=list(self._mod_server_names.keys()), width=22)
        self.mod_server_combo.pack(side=tk.LEFT, padx=6)
        self.mod_server_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._mod_on_server_change())
        ttk.Label(top, text="バージョン:").pack(side=tk.LEFT, padx=(8, 0))
        self.mod_ver_var = tk.StringVar()
        self.mod_ver_combo = ttk.Combobox(
            top, textvariable=self.mod_ver_var, width=10)
        self.mod_ver_combo.pack(side=tk.LEFT, padx=6)
        self.mod_ver_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._mod_refresh_library())
        self.mod_ver_combo.bind("<Return>",
                                lambda _e: self._mod_refresh_library())
        ttk.Button(top, text="📂 modlibフォルダを開く",
                   command=self._mod_open_folder).pack(side=tk.RIGHT)

        mid = ttk.Frame(parent)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 左: サーバー上の導入済みMOD
        left = ttk.LabelFrame(mid, text="導入済みMOD(サーバー上)")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.mod_installed = tk.Listbox(
            left, selectmode=tk.EXTENDED, activestyle="none", height=12,
            bg=PAL["surface"], fg=PAL["text"], selectbackground=PAL["accent"],
            selectforeground=PAL["on_accent"], relief=tk.FLAT, borderwidth=0)
        self.mod_installed.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 0))
        lb = ttk.Frame(left)
        lb.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(lb, text="🔄 更新",
                   command=self._mod_refresh_installed).pack(side=tk.LEFT)
        ttk.Button(lb, text="➖ 選択を削除",
                   command=self._mod_remove).pack(side=tk.RIGHT)

        # 右: ホスト側ライブラリ(ばらMOD + modpack)
        right = ttk.Frame(mid)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        loose_f = ttk.LabelFrame(right, text="ライブラリのMOD(選択バージョン)")
        loose_f.pack(fill=tk.BOTH, expand=True)
        self.mod_library = tk.Listbox(
            loose_f, selectmode=tk.EXTENDED, activestyle="none", height=7,
            bg=PAL["surface"], fg=PAL["text"], selectbackground=PAL["accent"],
            selectforeground=PAL["on_accent"], relief=tk.FLAT, borderwidth=0)
        self.mod_library.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 0))
        lib_b = ttk.Frame(loose_f)
        lib_b.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(lib_b, text="🔄 更新",
                   command=self._mod_refresh_library).pack(side=tk.LEFT)
        ttk.Button(lib_b, text="➕ 選択を追加",
                   command=self._mod_add).pack(side=tk.RIGHT)

        pack_f = ttk.LabelFrame(right, text="modpack(選択バージョン)")
        pack_f.pack(fill=tk.X, pady=(8, 0))
        pr = ttk.Frame(pack_f)
        pr.pack(fill=tk.X, padx=6, pady=6)
        self.mod_pack_var = tk.StringVar()
        self.mod_pack_combo = ttk.Combobox(
            pr, textvariable=self.mod_pack_var, state="readonly", width=20)
        self.mod_pack_combo.pack(side=tk.LEFT)
        self.mod_prune_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(pr, text="pack外を削除(完全同期)",
                        variable=self.mod_prune_var).pack(side=tk.LEFT, padx=8)
        ttk.Button(pr, text="📦 適用",
                   command=self._mod_apply_pack).pack(side=tk.RIGHT)

        out = ttk.LabelFrame(parent, text="出力")
        out.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.mod_text = tk.Text(
            out, wrap=tk.WORD, height=6, state=tk.DISABLED,
            font=("Consolas", 9), bg="#20262f", fg="#d7dce6",
            relief=tk.FLAT, borderwidth=0, padx=8, pady=6)
        self.mod_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self.mod_server_combo.current(0)
        self._mod_refresh_library()
        # 進捗バー等の生成後に、サーバーのバージョン反映+導入済み取得
        self.after(2000, self._mod_on_server_change)

    def _mod_log(self, text: str) -> None:
        self.mod_text.configure(state=tk.NORMAL)
        self.mod_text.insert(tk.END, text + "\n")
        self.mod_text.see(tk.END)
        self.mod_text.configure(state=tk.DISABLED)

    def _mod_selected_profile(self):
        name = self._mod_server_names.get(self.mod_server_var.get())
        if not name or name not in self.servers:
            messagebox.showinfo("選択なし", "対象サーバーを選んでください")
            return None
        return self.servers[name].profile

    def _mod_confirm_restart(self, profile, action: str) -> bool:
        return messagebox.askyesno(
            "確認",
            f"{profile.display_name} で {action} します。\n\n"
            "反映のためサーバーを再起動するので、接続中のプレイヤーは"
            "一度切断されます。続行しますか?")

    def _mod_on_server_change(self) -> None:
        """対象サーバーが変わったら、そのサーバーのバージョンに切り替えて再表示。"""
        profile = self._mod_selected_profile()
        if profile is None:
            return
        ver = self._versions.get(profile.name, "")
        if ver and ver != "?":
            self.mod_ver_var.set(ver)
        self._mod_refresh_library()
        self._mod_refresh_installed()

    def _mod_refresh_library(self) -> None:
        self.modlib.ensure_dirs()
        ver = self.mod_ver_var.get().strip()
        # バージョンの選択肢(既存フォルダ + 現在値)を更新
        versions = self.modlib.versions()
        if ver and ver not in versions:
            versions = sorted(set(versions) | {ver})
        self.mod_ver_combo.configure(values=versions)
        self.mod_library.delete(0, tk.END)
        self.mod_pack_combo.configure(values=[])
        if not ver:
            return
        self.modlib.ensure_version(ver)
        for name in self.modlib.loose_mods(ver):
            self.mod_library.insert(tk.END, name)
        packs = self.modlib.packs(ver)
        self.mod_pack_combo.configure(values=packs)
        if packs and self.mod_pack_var.get() not in packs:
            self.mod_pack_combo.current(0)

    def _mod_refresh_installed(self) -> None:
        profile = self._mod_selected_profile()
        if profile is None:
            return
        self.mod_installed.delete(0, tk.END)
        self.mod_installed.insert(tk.END, "(取得中…)")
        self._begin_busy(f"{profile.display_name}: MOD一覧を取得中…")

        def job():
            return modmanager.list_installed(profile)

        def on_done(mods, error):
            self._end_busy()
            self.mod_installed.delete(0, tk.END)
            if error is not None:
                self._set_status(f"エラー: {error}")
                self._mod_log(f"[取得エラー] {error}")
                return
            for m in mods:
                self.mod_installed.insert(tk.END, m)
            self._set_status(f"{profile.display_name}: 導入済みMOD {len(mods)}個")

        self._submit(job, on_done)

    def _mod_add(self) -> None:
        profile = self._mod_selected_profile()
        if profile is None:
            return
        sel = [self.mod_library.get(i) for i in self.mod_library.curselection()]
        if not sel:
            messagebox.showinfo("選択なし",
                                "追加するMODをライブラリから選んでください")
            return
        ver = self.mod_ver_var.get().strip()
        if not ver:
            messagebox.showinfo("バージョン未選択", "バージョンを選択してください")
            return
        if not self._mod_confirm_restart(profile, f"{len(sel)}個のMODを追加"):
            return
        paths = [self.modlib.loose_path(ver, n) for n in sel]
        self._begin_busy(f"{profile.display_name}: MOD追加中…")

        def job():
            modmanager.add_mods(profile, paths, restart=True,
                                progress=self._progress_from_worker)

        self._task_submit(f"{profile.display_name}: MOD追加 {len(sel)}個",
                          job, self._mod_action_done(
                              profile, f"{len(sel)}個のMODを追加+再起動しました"),
                          category="MOD追加", busy=False)

    def _mod_remove(self) -> None:
        profile = self._mod_selected_profile()
        if profile is None:
            return
        sel = [self.mod_installed.get(i)
               for i in self.mod_installed.curselection()]
        sel = [s for s in sel if s.lower().endswith(".jar")]
        if not sel:
            messagebox.showinfo("選択なし", "削除するMODを一覧から選んでください")
            return
        if not self._mod_confirm_restart(profile, f"{len(sel)}個のMODを削除"):
            return
        self._begin_busy(f"{profile.display_name}: MOD削除中…")

        def job():
            modmanager.remove_mods(profile, sel, restart=True,
                                   progress=self._progress_from_worker)

        self._task_submit(f"{profile.display_name}: MOD削除 {len(sel)}個",
                          job, self._mod_action_done(
                              profile, f"{len(sel)}個のMODを削除+再起動しました"),
                          category="MOD削除", busy=False)

    def _mod_apply_pack(self) -> None:
        profile = self._mod_selected_profile()
        if profile is None:
            return
        ver = self.mod_ver_var.get().strip()
        pack = self.mod_pack_var.get()
        if not ver or not pack:
            messagebox.showinfo("選択なし",
                                "バージョンと適用するmodpackを選んでください")
            return
        prune = self.mod_prune_var.get()
        msg = f"modpack「{pack}」を適用"
        if prune:
            msg += "(pack外のMODは削除=完全同期)"
        if not self._mod_confirm_restart(profile, msg):
            return
        self._begin_busy(f"{profile.display_name}: modpack適用中…")

        def job():
            return modmanager.apply_pack(
                profile, self.modlib, ver, pack, prune=prune, restart=True,
                progress=self._progress_from_worker)

        def on_done(result, error):
            self._end_busy()
            if error is not None:
                self._set_status(f"エラー: {error}")
                self._mod_log(f"[適用エラー] {error}")
            else:
                line = f"[modpack {pack}] 追加 {len(result['added'])}個"
                if result["removed"]:
                    line += f" / 削除 {len(result['removed'])}個"
                self._mod_log(line)
                self._set_status(f"modpack「{pack}」を適用しました")
            self._mod_refresh_installed()

        self._task_submit(f"{profile.display_name}: modpack「{pack}」適用",
                          job, on_done, category="modpack適用", busy=False)

    def _mod_action_done(self, profile, msg: str):
        def on_done(_result, error):
            self._end_busy()
            if error is not None:
                self._set_status(f"エラー: {error}")
                self._mod_log(f"[エラー] {error}")
            else:
                self._set_status(f"{profile.display_name}: {msg}")
                self._mod_log(f"{profile.display_name}: {msg}")
            self._mod_refresh_installed()
        return on_done

    def _mod_open_folder(self) -> None:
        import os
        self.modlib.ensure_dirs()
        ver = self.mod_ver_var.get().strip()
        if ver:
            self.modlib.ensure_version(ver)
            target = self.modlib.version_dir(ver)
        else:
            target = self.modlib.root
        try:
            os.startfile(str(target))
        except Exception as exc:
            messagebox.showerror("エラー", f"フォルダを開けません: {exc}")

    # ---------- ARK(ホスト)タブ ----------

    def _build_ark_tab(self, parent: ttk.Frame) -> None:
        # 更新バー(SteamCMD)。更新があると分かるように状態を表示。
        if self.arkupdate_enabled:
            upbar = ttk.Frame(parent)
            upbar.pack(fill=tk.X, padx=8, pady=(8, 0))
            self.ark_update_var = tk.StringVar(value="サーバー更新: 確認中…")
            self.ark_update_lbl = ttk.Label(upbar, textvariable=self.ark_update_var)
            self.ark_update_lbl.pack(side=tk.LEFT)
            ttk.Button(upbar, text="⬆ サーバー更新",
                       command=self._ark_update_now).pack(side=tk.RIGHT)
            ttk.Button(upbar, text="🔍 更新確認",
                       command=lambda: self._ark_update_check(silent=False)).pack(
                side=tk.RIGHT, padx=6)

        top = ttk.LabelFrame(parent, text="🦖 ARK サーバー(ホスト・複数マップ対応)")
        top.pack(fill=tk.X, padx=8, pady=(8, 0))
        h = min(7, max(3, len(self.arkhosts)))
        self.ark_tree = ttk.Treeview(top, columns=("status", "public", "players"),
                                     show="tree headings", height=h)
        self.ark_tree.heading("#0", text="サーバー")
        self.ark_tree.heading("status", text="状態")
        self.ark_tree.heading("public", text="外部公開")
        self.ark_tree.heading("players", text="人数")
        self.ark_tree.column("#0", width=280)
        self.ark_tree.column("status", width=100, anchor=tk.CENTER)
        self.ark_tree.column("public", width=90, anchor=tk.CENTER)
        self.ark_tree.column("players", width=70, anchor=tk.CENTER)
        self.ark_tree.pack(fill=tk.X, padx=6, pady=(6, 0))
        for i, ah in enumerate(self.arkhosts):
            port = ah.cfg.game_port
            label = ah.cfg.display_name + (f"  (:{port})" if port else "")
            self.ark_tree.insert("", tk.END, iid=str(i), text=label,
                                 values=("確認中…", "…", "-"))
        for tag, color in TAG_COLORS.items():
            self.ark_tree.tag_configure(tag, foreground=color)

        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(btns, text="▶ 起動", command=self._ark_start).pack(side=tk.LEFT)
        ttk.Button(btns, text="⏹ 停止", command=self._ark_stop).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="🔁 再起動", command=self._ark_restart).pack(side=tk.LEFT)
        ttk.Button(btns, text="⚙ 詳細設定",
                   command=self._ark_server_config).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="⚡ dynamic設定",
                   command=self._ark_dynconfig).pack(side=tk.LEFT)
        ttk.Button(btns, text="💾 バックアップ/復元",
                   command=self._ark_backup_open).pack(side=tk.LEFT, padx=6)
        self.ark_console_btn = ttk.Button(
            btns, text="🖥 コンソール", command=self._ark_toggle_console)
        self.ark_console_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="🔄 更新", command=self._ark_refresh).pack(side=tk.RIGHT)

        # 表示欄: 通常はRCON/ゲームログ、コンソールONでサーバー起動ログ(ファイル)を追尾
        self._ark_console_mode = False
        self.ark_info_frame = ttk.LabelFrame(parent, text="RCON出力(選択中サーバー)")
        self.ark_info_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.ark_info = tk.Text(self.ark_info_frame, wrap=tk.NONE, height=9,
                                state=tk.DISABLED, font=("Consolas", 9),
                                bg="#20262f", fg="#d7dce6",
                                relief=tk.FLAT, borderwidth=0, padx=8, pady=6)
        self.ark_info.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # クイックコマンド(選択マップへRCONで即実行)
        quick = ttk.Frame(parent)
        quick.pack(fill=tk.X, padx=8, pady=(0, 2))
        ttk.Label(quick, text="クイック:", foreground=PAL["muted"]).pack(side=tk.LEFT)
        ARK_QUICK = [
            ("💾 保存", "saveworld", None),
            ("☀ 昼", "settimeofday 08:00", None),
            ("🌙 夜", "settimeofday 20:00", None),
            ("🦕 恐竜リスポーン", "DestroyWildDinos",
             "野生恐竜を全消去してリスポーンさせますか?(テイム/建築物は影響なし)"),
            ("💬 全体メッセージ", "__chat__", None),
        ]
        for label, cmd, confirm in ARK_QUICK:
            ttk.Button(quick, text=label,
                       command=lambda c=cmd, cf=confirm: self._ark_quick(c, cf)
                       ).pack(side=tk.LEFT, padx=2)

        rcon_bar = ttk.Frame(parent)
        rcon_bar.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Label(rcon_bar, text="RCON(選択サーバー) >").pack(side=tk.LEFT)
        self.ark_rcon_entry = ttk.Entry(rcon_bar)
        self.ark_rcon_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.ark_rcon_entry.bind("<Return>", lambda _e: self._ark_rcon_send())
        ttk.Button(rcon_bar, text="送信", command=self._ark_rcon_send).pack(side=tk.LEFT)

        # 右クリックメニュー(単発の野生恐竜リスポーン等)
        self.ark_menu = tk.Menu(self, tearoff=0)
        self.ark_menu.add_command(label="🦕 野生恐竜をリスポーン(DestroyWildDinos)",
                                  command=self._ark_respawn_dinos)
        self.ark_tree.bind("<Button-3>",
                           lambda e: self._popup_menu(e, self.ark_tree, self.ark_menu))

        # サーバーごとのログバッファ(起動中はRCON応答+ゲームログを蓄積して保持)
        self._ark_logs = ["" for _ in self.arkhosts]
        self.ark_tree.bind("<<TreeviewSelect>>", lambda _e: self._ark_on_select())
        if self.arkhosts:
            self.ark_tree.selection_set("0")
        self.after(1200, self._ark_refresh)
        self.after(30000, self._ark_auto_refresh)   # 30秒毎に状態+ゲームログを更新
        self.after(1500, self._ark_console_loop)    # コンソールON時にログファイルを追尾

    def _ark_on_select(self) -> None:
        """選択が変わったとき、モードに応じて表示欄を更新する。"""
        if self._ark_console_mode:
            self._ark_console_update()
        else:
            self._ark_show_log()

    def _ark_toggle_console(self) -> None:
        """表示欄をRCONログ ⇔ サーバーコンソール(起動ログ)で切り替える。"""
        self._ark_console_mode = not self._ark_console_mode
        if self._ark_console_mode:
            self.ark_console_btn.configure(text="🖥 コンソール: ON")
            self.ark_info_frame.configure(
                text="サーバーコンソール(選択マップの起動ログ・3秒毎に自動更新)")
            self._ark_console_update()
        else:
            self.ark_console_btn.configure(text="🖥 コンソール")
            self.ark_info_frame.configure(text="RCON出力(選択中サーバー)")
            self._ark_show_log()

    def _ark_console_loop(self) -> None:
        """コンソールON中は選択マップのログファイル末尾を定期的に取り込む。"""
        if self._ark_console_mode:
            self._ark_console_update()
        self.after(3000, self._ark_console_loop)

    def _ark_console_update(self) -> None:
        sel = self.ark_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        ah = self.arkhosts[idx]

        def on_done(text, error):
            if not self._ark_console_mode:
                return
            cur = self.ark_tree.selection()
            if not cur or int(cur[0]) != idx:   # 表示中に選択が変わっていたら破棄
                return
            body = text if error is None else f"(コンソール取得失敗: {error})"
            # スクロールが末尾付近なら追従、そうでなければ位置を保つ
            at_bottom = self.ark_info.yview()[1] > 0.999
            self.ark_info.configure(state=tk.NORMAL)
            self.ark_info.delete("1.0", tk.END)
            self.ark_info.insert(tk.END, body)
            if at_bottom:
                self.ark_info.see(tk.END)
            self.ark_info.configure(state=tk.DISABLED)

        self._submit(lambda: ah.tail_log(400), on_done)

    def _ark_show_log(self) -> None:
        """選択中サーバーの保持ログを表示欄に反映する。"""
        sel = self.ark_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        self.ark_info.configure(state=tk.NORMAL)
        self.ark_info.delete("1.0", tk.END)
        self.ark_info.insert(tk.END, self._ark_logs[idx])
        self.ark_info.see(tk.END)
        self.ark_info.configure(state=tk.DISABLED)

    def _ark_append_log(self, idx: int, text: str) -> None:
        """サーバーidxのログバッファに追記(表示中なら画面にも反映)。末尾40000字に丸める。"""
        buf = self._ark_logs[idx] + text
        if len(buf) > 40000:
            buf = buf[-40000:]
        self._ark_logs[idx] = buf
        if self._ark_console_mode:      # コンソール表示中はRCONログで上書きしない
            return
        sel = self.ark_tree.selection()
        if sel and int(sel[0]) == idx:
            self.ark_info.configure(state=tk.NORMAL)
            self.ark_info.insert(tk.END, text)
            self.ark_info.see(tk.END)
            self.ark_info.configure(state=tk.DISABLED)

    def _ark_auto_refresh(self) -> None:
        self._ark_refresh(silent=True)
        self.after(30000, self._ark_auto_refresh)

    def _ark_selected(self):
        sel = self.ark_tree.selection()
        if not sel:
            messagebox.showinfo("選択なし", "ARKサーバーを選択してください")
            return None
        return self.arkhosts[int(sel[0])]

    @staticmethod
    def _ark_player_count(players_raw: str) -> str:
        import re
        if not players_raw:
            return "-"
        if "No Players" in players_raw:
            return "0"
        if players_raw.startswith("(") or "RCON" in players_raw:
            return "-"
        return str(len(re.findall(r'(?m)^\s*\d+\.\s', players_raw)))

    def _ark_refresh(self, silent: bool = False) -> None:
        if not silent:
            self._begin_busy("ARK状態を取得中…")

        def job():
            out = []
            for ah in self.arkhosts:
                gamelog = ""
                try:
                    running = ah.is_running()
                    players = ah.players() if running else ""
                    if running:               # 起動中はゲームログ(getgamelog)を取り込む
                        try:
                            gl = ah.rcon_command("getgamelog")
                            low = (gl or "").lower()
                            if gl and "no response" not in low and "no new" not in low:
                                gamelog = gl.strip()
                        except Exception:
                            pass
                except Exception as exc:
                    running, players = False, f"(err {exc})"
                out.append((running, players, gamelog))
            return out

        def on_done(results, error):
            if not silent:
                self._end_busy()
            if error is not None:
                if not silent:
                    self._set_status(f"ARK取得失敗: {error}")
                return
            first_seen = False
            for i, (running, players, gamelog) in enumerate(results):
                pc = self._ark_player_count(players) if running else "-"
                # public列は外部公開監視が更新するので触らない(set で個別更新)
                self.ark_tree.set(str(i), "status",
                                  "🟢 稼働中" if running else "⚪ 停止中")
                self.ark_tree.set(str(i), "players", pc)
                self.ark_tree.item(str(i), tags=("active" if running else "off",))
                prev = self._ark_running.get(i)           # None=初回(未取得)
                self._ark_running[i] = running
                if prev is None:
                    first_seen = True                     # 初回は開閉のみ
                elif prev != running:                     # 変化: ポート+通知+クラッシュ判定
                    self._server_state_changed(
                        f"ark:{i}", "ark", self.arkhosts[i].cfg.display_name,
                        running, self.arkhosts[i])
                if gamelog:
                    self._ark_append_log(i, gamelog + "\n")
            if first_seen:
                self._portsync_on_change()
            if not silent:
                self._set_status("ARK状態を更新しました")

        self._submit(job, on_done)

    def _ark_start(self) -> None:
        ah = self._ark_selected()
        if ah is None:
            return
        if not messagebox.askyesno(
                "確認", f"{ah.cfg.display_name} を起動しますか?\n"
                "(このマップが起動中なら二重起動しません)"):
            return
        self._begin_busy(f"{ah.cfg.display_name} 起動中…")
        self._task_submit(f"{ah.cfg.display_name} を起動",
                          lambda: ah.start(progress=self._progress_from_worker),
                          self._ark_action_done(f"{ah.cfg.display_name} を起動しました", 4000),
                          category="ARK起動", busy=False)

    def _ark_stop(self) -> None:
        ah = self._ark_selected()
        if ah is None:
            return
        if not messagebox.askyesno(
                "⏹ ARK停止",
                f"{ah.cfg.display_name} を停止しますか?\n\n"
                "ワールドを保存(saveworld)してから終了します。\n"
                "プレイヤーが居ればチャットで60/30/10秒前に予告します"
                "(不在なら即停止)。",
                icon="warning", default="no"):
            return
        self._mark_stop(f"ark:{self.arkhosts.index(ah)}")
        self._begin_busy(f"{ah.cfg.display_name} 停止中…")
        self._task_submit(
            f"{ah.cfg.display_name} を停止",
            lambda: ah.stop_with_notice(progress=self._progress_from_worker),
            self._ark_action_done(f"{ah.cfg.display_name} を停止しました(保存済み)", 2000),
            category="ARK停止", busy=False)

    def _ark_restart(self) -> None:
        ah = self._ark_selected()
        if ah is None:
            return
        respawn = self.ark_respawn_on_restart     # ⚙詳細設定のトグルで決まる
        extra = "\n🦕 詳細設定ONのため、再起動後に野生恐竜をリスポーンします。" if respawn else ""
        if not messagebox.askyesno(
                "🔁 ARK再起動",
                f"{ah.cfg.display_name} を再起動しますか?\n\n"
                "保存→終了→起動します。プレイヤーが居ればチャットで60/30/10秒前に予告します"
                "(不在なら即再起動)。" + extra,
                icon="warning", default="no"):
            return
        self._mark_restart(f"ark:{self.arkhosts.index(ah)}")
        self._begin_busy(f"{ah.cfg.display_name} 再起動中…")
        title = f"{ah.cfg.display_name} を再起動" + ("+恐竜リスポーン" if respawn else "")
        self._task_submit(
            title,
            lambda: ah.restart_with_notice(respawn_dinos=respawn,
                                           progress=self._progress_from_worker),
            self._ark_action_done(f"{ah.cfg.display_name} を再起動しました", 4000),
            category="ARK再起動", busy=False)

    def _ark_respawn_dinos(self) -> None:
        """選択中の稼働マップの野生恐竜を今すぐリスポーン(DestroyWildDinos)。"""
        ah = self._ark_selected()
        if ah is None:
            return
        if not messagebox.askyesno(
                "🦕 野生恐竜リスポーン",
                f"{ah.cfg.display_name} の野生恐竜を全消去してリスポーンさせますか?\n\n"
                "・テイム済み恐竜、建築物、プレイヤーには影響しません\n"
                "・消えた分は時間とともに新しく湧き直します(DestroyWildDinos)",
                icon="warning", default="no"):
            return

        def job():
            if not ah.is_running():
                return "not_running"
            ah.destroy_wild_dinos()
            return "ok"

        def on_done(res, error):
            if error is not None:
                self._set_status(f"リスポーンエラー: {error}")
                messagebox.showerror("エラー", str(error))
                return
            if res == "not_running":
                self._set_status(f"{ah.cfg.display_name} は停止中のため実行できません")
            else:
                self._set_status(f"{ah.cfg.display_name} の野生恐竜をリスポーンしました")

        self._task_submit(f"{ah.cfg.display_name}: 野生恐竜リスポーン",
                          job, on_done, category="恐竜リスポーン", busy=True)

    def _ark_action_done(self, msg: str, refresh_ms: int):
        def on_done(_r, error):
            self._end_busy()
            if error is not None:
                self._set_status(f"ARKエラー: {error}")
                messagebox.showerror("ARK", str(error))
            else:
                self._set_status(msg)
            self.after(refresh_ms, self._ark_refresh)
        return on_done

    def _ark_rcon_send(self) -> None:
        sel = self.ark_tree.selection()
        if not sel:
            messagebox.showinfo("選択なし", "ARKサーバーを選択してください")
            return
        idx = int(sel[0])
        ah = self.arkhosts[idx]
        cmd = self.ark_rcon_entry.get().strip()
        if not cmd:
            return
        self.ark_rcon_entry.delete(0, tk.END)
        self._begin_busy(f"RCON({ah.cfg.display_name}): {cmd}")

        def job():
            return ah.rcon_command(cmd)

        def on_done(resp, error):
            self._end_busy()
            body = str(error) if error is not None else (resp.strip() or "(応答なし)")
            self._ark_append_log(idx, f"> {cmd}\n{body}\n\n")

        self._submit(job, on_done)

    def _ark_quick(self, cmd: str, confirm: str | None) -> None:
        """クイックコマンド: 選択マップへRCONで即実行。"""
        ah = self._ark_selected()
        if ah is None:
            return
        idx = self.arkhosts.index(ah)
        if cmd == "__chat__":                    # 全体メッセージ(ServerChat)
            msg = simpledialog.askstring(
                "全体メッセージ", "チャットに送る内容(英数字推奨):", parent=self)
            if not msg:
                return
            cmd = "ServerChat " + msg
        if confirm and not messagebox.askyesno("確認", confirm,
                                               icon="warning", default="no"):
            return
        self._begin_busy(f"RCON({ah.cfg.display_name}): {cmd}")

        def job():
            if not ah.is_running():
                return "(停止中)"
            return ah.rcon_command(cmd)

        def on_done(resp, error):
            self._end_busy()
            body = str(error) if error is not None else (resp.strip() or "(応答なし)")
            self._ark_append_log(idx, f"> {cmd}\n{body}\n\n")
            self._set_status(f"{ah.cfg.display_name}: {cmd} → {body[:40]}")

        self._submit(job, on_done)

    # ---------- ARKサーバー更新(SteamCMD) ----------

    def _ark_installs(self) -> dict:
        """install_root(str) -> そのインストールを使うマップindexのリスト。"""
        groups = {}
        for idx, ah in enumerate(self.arkhosts):
            groups.setdefault(str(ah.cfg.install_root), []).append(idx)
        return groups

    def _ark_update_check(self, silent: bool = True) -> None:
        """各インストールの導入buildと最新buildを比較して表示(+更新ありなら通知)。"""
        if not self.arkupdate_enabled:
            return
        sc = self.ark_steamcmd
        roots = list(self._ark_installs().keys())
        if not silent:
            self._set_status("ARK更新を確認中…(SteamCMD)")

        def job():
            latest = arkupdate.latest_buildid(sc)
            builds = {r: arkupdate.installed_buildid(r) for r in roots}
            return {"latest": latest, "builds": builds}

        def on_done(res, error):
            if error is not None:
                if hasattr(self, "ark_update_var"):
                    self.ark_update_var.set("サーバー更新: 確認失敗")
                if not silent:
                    self._set_status(f"ARK更新確認に失敗: {error}")
                return
            self._ark_update_latest = res["latest"]
            self._ark_update_builds = res["builds"]
            latest = res["latest"]
            outdated = [r for r, b in res["builds"].items() if b and latest and b != latest]
            if not hasattr(self, "ark_update_var"):
                return
            if outdated:
                self.ark_update_var.set(
                    f"🆕 更新あり  {len(outdated)}インストール (最新 build {latest})")
                self.ark_update_lbl.configure(foreground=PAL["busy"])
                if getattr(self, "_ark_update_notified", None) != latest:
                    self._ark_update_notified = latest
                    self._notify("update",
                                 f"🆕 ARKサーバーに更新があります(最新 build {latest})\n"
                                 f"更新が必要なインストール: {len(outdated)}")
            else:
                self.ark_update_var.set(f"✅ 全て最新  (build {latest or '?'})")
                self.ark_update_lbl.configure(foreground=PAL["ok"])
            if not silent:
                self._set_status("ARK更新の確認が完了しました")

        self._submit(job, on_done)

    def _ark_update_auto_check(self) -> None:
        if self.arkupdate_enabled:
            self._ark_update_check(silent=True)
        self.after(6 * 3600 * 1000, self._ark_update_auto_check)   # 6時間ごと

    def _ark_update_now(self) -> None:
        """更新するサーバー(マップ)を選んで更新。各インストール単位で停止→更新→再起動。"""
        if not self.arkupdate_enabled:
            return
        sc = self.ark_steamcmd
        latest = self._ark_update_latest
        builds = self._ark_update_builds
        dlg = tk.Toplevel(self)
        dlg.title("⬆ ARKサーバー更新")
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("520x560+%d+%d" % (self.winfo_rootx() + 160, self.winfo_rooty() + 80))
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text="更新するサーバー(マップ)を選択:").pack(anchor=tk.W)
        ttk.Label(frm, foreground=PAL["muted"], justify=tk.LEFT,
                  text="各サーバーは個別インストール。選んだものだけ、停止→更新→(任意で)再起動します。\n"
                       "同じインストールを共有するマップがある場合はまとめて停止/更新します。").pack(
            anchor=tk.W, pady=(2, 6))

        listwrap = ttk.Frame(frm)
        listwrap.pack(fill=tk.BOTH, expand=True, pady=(2, 6))
        canvas = tk.Canvas(listwrap, highlightthickness=0, bg=PAL["bg"], height=260)
        vsb = ttk.Scrollbar(listwrap, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        map_vars = []
        for i, ah in enumerate(self.arkhosts):
            root = str(ah.cfg.install_root)
            b = builds.get(root)
            outdated = bool(b and latest and b != latest)
            tag = "🆕更新あり" if outdated else ("✅最新" if b else "?")
            run = "  (稼働中)" if self._ark_running.get(i) else ""
            v = tk.BooleanVar(value=outdated)     # 既定: 更新が必要なものだけ
            ttk.Checkbutton(inner, variable=v,
                            text=f"{ah.cfg.display_name}  [{tag} build {b or '?'}]{run}").pack(
                anchor=tk.W, padx=4)
            map_vars.append((i, v))

        sel = ttk.Frame(frm)
        sel.pack(fill=tk.X)
        ttk.Button(sel, text="全選択", command=lambda: [v.set(True) for _i, v in map_vars]).pack(side=tk.LEFT)
        ttk.Button(sel, text="全解除", command=lambda: [v.set(False) for _i, v in map_vars]).pack(side=tk.LEFT, padx=6)
        restart_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, variable=restart_var,
                        text="更新後、停止したマップを起動し直す").pack(anchor=tk.W, pady=(6, 0))

        def go():
            chosen = [i for i, v in map_vars if v.get()]
            do_restart = restart_var.get()
            dlg.destroy()
            if not chosen:
                self._set_status("更新するサーバーが選ばれていません")
                return
            groups = self._ark_installs()
            roots = []
            for i in chosen:
                r = str(self.arkhosts[i].cfg.install_root)
                if r not in roots:
                    roots.append(r)

            def job():
                for root in roots:
                    idxs = groups.get(root, [])
                    running_here = [k for k in idxs if self.arkhosts[k].is_running()]
                    for k in running_here:
                        ah = self.arkhosts[k]
                        self._mark_stop(f"ark:{k}")
                        self._progress_from_worker(f"{ah.cfg.display_name} を停止中…")
                        ah.stop(progress=self._progress_from_worker)
                    self._progress_from_worker(f"更新中: {root}")
                    arkupdate.update(sc, root, progress=self._progress_from_worker)
                    if do_restart:
                        for k in running_here:
                            ah = self.arkhosts[k]
                            self._mark_restart(f"ark:{k}")
                            self._progress_from_worker(f"{ah.cfg.display_name} を起動中…")
                            try:
                                ah.start(progress=self._progress_from_worker)
                            except Exception as e:
                                self._progress_from_worker(f"{ah.cfg.display_name} 起動失敗: {e}")
                return len(roots)

            def on_done(n, error):
                self._end_busy()
                if error is not None:
                    self._set_status(f"ARK更新に失敗: {error}")
                    self._notify("update", f"❌ ARKサーバー更新に失敗: {error}")
                    messagebox.showerror("更新失敗", str(error))
                    return
                self._set_status(f"ARKサーバーを更新しました({n}インストール)")
                self._notify("update", f"✅ ARKサーバーを更新しました({n}インストール)")
                self.after(3000, lambda: self._ark_update_check(silent=True))
                self.after(4000, self._ark_refresh)

            self._begin_busy("ARKサーバーを更新中…(SteamCMD)")
            self._task_submit("⬆ ARKサーバー更新(SteamCMD)", job, on_done,
                              category="サーバー更新", busy=False)

        bar = ttk.Frame(frm)
        bar.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(bar, text="更新する", command=go).pack(side=tk.RIGHT, padx=6)
        ttk.Button(bar, text="キャンセル", command=dlg.destroy).pack(side=tk.RIGHT)

    # ---------- バックアップ / 復元 ----------

    def _open_backup_dialog(self, title, target, backup_job, restore_job, stop_note):
        cfg = self.backupcfg
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("580x420+%d+%d"
                        % (self.winfo_rootx() + 160, self.winfo_rooty() + 80))
        frm = ttk.Frame(dialog, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, foreground=PAL["muted"],
                  text=f"保存先: {cfg.path}\\{target}  (世代 {cfg.keep} / 圧縮)"
                  ).pack(anchor=tk.W)
        tree = ttk.Treeview(frm, columns=("size", "date"),
                            show="tree headings", height=11)
        tree.heading("#0", text="バックアップ")
        tree.heading("size", text="サイズ")
        tree.heading("date", text="日時")
        tree.column("#0", width=290)
        tree.column("size", width=90, anchor=tk.CENTER)
        tree.column("date", width=140, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, pady=6)

        def refresh_list():
            if not dialog.winfo_exists():
                return
            tree.delete(*tree.get_children())
            for b in backup.list_backups(cfg, target):
                tree.insert("", tk.END, iid=b["path"], text=b["name"],
                            values=(f"{b['size_mb']}MB", b["mtime"]))
        refresh_list()

        def do_backup():
            self._begin_busy(f"{target}: バックアップ中…")

            def on_done(_r, error):
                self._end_busy()
                if error is not None:
                    self._set_status(f"バックアップ失敗: {error}")
                    messagebox.showerror("バックアップ", str(error))
                else:
                    self._set_status(f"{target}: バックアップ完了")
                    self._notify("backup", f"💾 {title} が完了しました")
                    refresh_list()
            self._task_submit(f"バックアップ: {title}",
                              lambda: backup_job(self._progress_from_worker), on_done,
                              category="バックアップ", busy=False)

        def do_restore():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("選択なし", "復元するバックアップを選んでください",
                                    parent=dialog)
                return
            bf = sel[0]
            if not messagebox.askyesno(
                    "復元の確認",
                    f"このバックアップで復元しますか?\n{tree.item(bf, 'text')}\n\n"
                    f"{stop_note}\n既存データは上書きされます。",
                    icon="warning", default="no", parent=dialog):
                return
            dialog.destroy()
            self._begin_busy(f"{target}: 復元中…")

            def on_done(_r, error):
                self._end_busy()
                if error is not None:
                    self._set_status(f"復元失敗: {error}")
                    messagebox.showerror("復元", str(error))
                else:
                    self._set_status(f"{target}: 復元完了(サーバーを起動してください)")
            self._task_submit(f"復元: {title}",
                              lambda: restore_job(bf, self._progress_from_worker), on_done,
                              category="復元", busy=False)

        def open_folder():
            import os
            d = Path(cfg.path) / target
            d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btns, text="💾 今すぐバックアップ",
                   command=do_backup).pack(side=tk.LEFT)
        ttk.Button(btns, text="♻ 選択を復元",
                   command=do_restore).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="📂 フォルダ", command=open_folder).pack(side=tk.LEFT)
        ttk.Button(btns, text="閉じる", command=dialog.destroy).pack(side=tk.RIGHT)

    def _ark_backup_open(self):
        ah = self._ark_selected()
        if ah is None:
            return
        saved_root = str(backup.ark_saved_dir(ah.cfg.config_dir))
        label = ah.cfg.map_label
        subdir = ah.cfg.save_subdir
        self._open_backup_dialog(
            f"ARK バックアップ / 復元: {ah.cfg.display_name}", f"ARK/{label}",
            backup_job=lambda pg: backup.ark_backup(
                saved_root, self.backupcfg, label, subdir, progress=pg),
            restore_job=lambda bf, pg: backup.ark_restore(bf, saved_root, progress=pg),
            stop_note=f"※ 復元前にこのマップ({ah.cfg.display_name})を停止してください"
                      "(このマップのセーブのみ上書きします)。")

    # ---------- ARK 詳細設定エディタ(日本語・GameUserSettings.ini/Game.ini) ----------

    def _ark_server_config(self) -> None:
        """ARKの詳細設定を日本語UIで編集する(全マップ共有の設定)。"""
        if not self.arkhosts:
            return
        config_dir = self.arkhosts[0].cfg.config_dir
        try:
            gus, game = arkconfig.load(config_dir)
        except Exception as exc:
            messagebox.showerror("読込エラー", f"ARK設定の読込に失敗: {exc}")
            return
        self._open_ark_config_dialog(config_dir, gus, game)

    def _ark_set_respawn(self, enabled: bool) -> None:
        """再起動時に野生恐竜をリスポーンするGSM設定のON/OFF(永続化)。"""
        self.ark_respawn_on_restart = enabled
        try:
            import json as _json
            ARKBEHAVIOR_PATH.write_text(
                _json.dumps({"respawn_on_restart": bool(enabled)}), encoding="utf-8")
        except Exception:
            pass
        self._set_status("ARK再起動時の恐竜リスポーン: " + ("ON" if enabled else "OFF"))

    def _scrollable_tab(self, notebook, label):
        """Notebookに縦スクロール可能なタブを追加し、中身を置く内側Frameを返す。"""
        outer = ttk.Frame(notebook)
        notebook.add(outer, text=label)
        canvas = tk.Canvas(outer, highlightthickness=0, bg=PAL["bg"])
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        wheel = lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
        inner.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        inner.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _open_ark_config_dialog(self, config_dir, gus, game) -> None:
        inis = {"gus": gus, "game": game}
        dialog = tk.Toplevel(self)
        dialog.title("ARK 詳細設定(全マップ共有)")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("700x640+%d+%d"
                        % (self.winfo_rootx() + 110, self.winfo_rooty() + 40))

        ttk.Label(
            dialog, foreground=PAL["muted"], justify=tk.LEFT, padding=(10, 8, 10, 0),
            text="⚠ この設定は全マップ共有(1インストール)です。反映には対象マップの再起動が必要。\n"
                 "　 各欄は現在値(未設定の項目はゲーム既定値を表示)。変更した項目だけ保存されます。"
        ).pack(anchor=tk.W)

        # GSMの再起動挙動(iniではなくGSM側の設定)
        behave = ttk.Frame(dialog, padding=(10, 2, 10, 0))
        behave.pack(fill=tk.X)
        respawn_toggle = tk.BooleanVar(value=self.ark_respawn_on_restart)
        ttk.Checkbutton(
            behave, variable=respawn_toggle,
            text="🦕 再起動時に野生恐竜をリスポーン(ONなら手動・予約どの再起動でも実行)",
            command=lambda: self._ark_set_respawn(respawn_toggle.get())
        ).pack(anchor=tk.W)

        nb = ttk.Notebook(dialog)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # field key -> (fk, section, key, kind, getter, initial_str)
        fields = []
        for tab_label, specs in ARK_SETTINGS_TABS:
            tab = self._scrollable_tab(nb, f" {tab_label} ")
            for row, (fk, section, key, kind, label, default) in enumerate(specs):
                cur = inis[fk].get(section, key)          # None=未設定
                # 表示値: ファイルにあればその値、無ければ既定値を実入力しておく
                shown = cur if cur is not None else default
                ttk.Label(tab, text=label, anchor=tk.W, wraplength=380).grid(
                    row=row, column=0, sticky=tk.W, padx=(8, 6), pady=3)
                if kind == "bool":
                    checked = (shown or "").strip().lower() == "true"
                    var = tk.BooleanVar(value=checked)
                    ttk.Checkbutton(tab, variable=var).grid(
                        row=row, column=1, sticky=tk.W, padx=6)
                    getter = lambda v=var: "True" if v.get() else "False"
                    initial = "True" if checked else "False"
                else:
                    var = tk.StringVar(value=shown or "")
                    ttk.Entry(tab, textvariable=var, width=12).grid(
                        row=row, column=1, sticky=tk.W, padx=6)
                    getter = lambda v=var: v.get().strip()
                    initial = shown or ""
                fields.append((fk, section, key, kind, getter, initial))
            tab.columnconfigure(0, weight=1)

        btns = ttk.Frame(dialog, padding=(8, 6))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="📄 生ファイル編集(上級者)",
                   command=lambda: self._ark_raw_edit(config_dir)).pack(side=tk.LEFT)
        restart_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="保存後に選択マップを再起動",
                        variable=restart_var).pack(side=tk.LEFT, padx=10)
        ttk.Button(btns, text="キャンセル", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="💾 保存",
                   command=lambda: _save()).pack(side=tk.RIGHT, padx=6)

        def _save() -> None:
            changed = {"gus": False, "game": False}
            applied = []
            for fk, section, key, kind, getter, initial in fields:
                val = getter()
                if val == initial:
                    continue                      # 触っていない項目は書かない
                if kind in ("float", "int"):
                    if val == "":
                        continue                  # 空欄化は削除しない(既定に任せる)
                    try:
                        float(val) if kind == "float" else int(val)
                    except ValueError:
                        messagebox.showerror(
                            "入力エラー",
                            f"「{key}」は{'整数' if kind=='int' else '数値'}で入力してください: {val}",
                            parent=dialog)
                        return
                inis[fk].set(section, key, val)
                changed[fk] = True
                applied.append(f"{key}={val}")
            if not applied:
                messagebox.showinfo("変更なし", "変更された項目はありません。", parent=dialog)
                return
            restart = restart_var.get()
            ah = None
            if restart:
                ah = self._ark_selected()
                if ah is None:                    # 選択が無ければ保存だけ
                    restart = False
            dialog.destroy()

            def job():
                if changed["gus"]:
                    inis["gus"].save()
                    self._progress_from_worker("GameUserSettings.ini を保存")
                if changed["game"]:
                    inis["game"].save()
                    self._progress_from_worker("Game.ini を保存")
                if restart and ah is not None:
                    self._progress_from_worker(f"{ah.cfg.display_name} を再起動(設定反映)…")
                    ah.restart_with_notice(progress=self._progress_from_worker)

            def on_done(_r, error):
                if error is not None:
                    self._set_status(f"ARK設定 保存エラー: {error}")
                    messagebox.showerror("保存エラー", str(error))
                    return
                extra = "(再起動済み)" if restart else "(反映には再起動が必要)"
                self._set_status(f"ARK設定を保存しました {extra}")
                self._append_log(
                    "■ ARK詳細設定を更新: " + ", ".join(applied) + f"\n  {extra}")
                if self.arkhosts:
                    self.after(3000, self._ark_refresh)

            title = "ARK詳細設定を保存" + ("+再起動" if restart else "")
            self._task_submit(title, job, on_done, category="ARK設定変更", busy=True)

    def _ark_raw_edit(self, config_dir) -> None:
        """GameUserSettings.ini / Game.ini を生テキストで直接編集する(上級者向け)。"""
        dialog = tk.Toplevel(self)
        dialog.title("ARK 生ファイル編集")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("720x620+%d+%d"
                        % (self.winfo_rootx() + 120, self.winfo_rooty() + 40))
        bar = ttk.Frame(dialog, padding=(8, 8, 8, 0))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="ファイル:").pack(side=tk.LEFT)
        file_var = tk.StringVar(value="GameUserSettings.ini")
        combo = ttk.Combobox(bar, textvariable=file_var, state="readonly", width=24,
                             values=["GameUserSettings.ini", "Game.ini"])
        combo.pack(side=tk.LEFT, padx=6)
        ttk.Label(bar, foreground=PAL["muted"],
                  text="※ 全文をそのまま上書きします。壊すと起動しなくなるので注意。").pack(side=tk.LEFT)

        txt = tk.Text(dialog, wrap=tk.NONE, font=("Consolas", 9),
                      bg=PAL["surface"], fg=PAL["text"], undo=True)
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        paths = {"GameUserSettings.ini": arkconfig.gus_path(config_dir),
                 "Game.ini": arkconfig.game_path(config_dir)}

        def load_file(*_):
            p = paths[file_var.get()]
            try:
                content = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
            except Exception as exc:
                content = f"(読込失敗: {exc})"
            txt.delete("1.0", tk.END)
            txt.insert(tk.END, content)
        combo.bind("<<ComboboxSelected>>", load_file)
        load_file()

        btns = ttk.Frame(dialog, padding=(8, 6))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="キャンセル", command=dialog.destroy).pack(side=tk.RIGHT)

        def save_raw():
            name = file_var.get()
            p = paths[name]
            content = txt.get("1.0", "end-1c")
            if not messagebox.askyesno(
                    "確認", f"{name} を上書き保存します。反映には再起動が必要です。続行しますか?",
                    parent=dialog):
                return
            try:
                p.write_text(content, encoding="utf-8")
            except Exception as exc:
                messagebox.showerror("保存エラー", str(exc), parent=dialog)
                return
            self._set_status(f"{name} を保存しました(反映には再起動が必要)")
            dialog.destroy()
        ttk.Button(btns, text="💾 このファイルを保存",
                   command=save_raw).pack(side=tk.RIGHT, padx=6)

    # ---------- ARK dynamic config(無停止で倍率を変更) ----------

    def _dyn_apply_flags(self) -> None:
        """全ARKに dynamic config の起動引数フラグ/URL を反映する。"""
        for ah in self.arkhosts:
            ah.cfg.use_dynamic_config = self.dynstate.enabled
            ah.cfg.dynamic_config_url = self.dynserver.url if self.dynstate.enabled else ""

    def _dyn_apply_initial(self) -> None:
        """起動時: 保存状態に合わせてフラグ設定＋配信ファイル生成＋(有効なら)HTTPサーバー起動。"""
        try:
            dynconfig.write_file(DYNFILE_PATH, self.dynstate.values)
        except Exception as exc:
            print("dynamicconfig.ini 書き込み失敗:", exc)
        if self.dynstate.enabled:
            try:
                self.dynserver.start()
            except Exception as exc:
                print("dynamic config HTTPサーバー起動失敗:", exc)
        self._dyn_apply_flags()

    def _dyn_set_enabled(self, enabled: bool) -> bool:
        """マスターON/OFF: HTTP配信の起動/停止＋起動引数フラグを切り替える(共有iniは触らない)。

        ASAはURLを起動引数 -CustomDynamicConfigUrl で受け取るため、有効化後は各マップを
        一度再起動して初めて反映される(そこは呼び出し側で案内する)。"""
        if not self.arkhosts:
            return False
        try:
            if enabled:
                self.dynserver.start()
            else:
                self.dynserver.stop()
        except Exception as exc:
            messagebox.showerror("dynamic config", f"HTTPサーバー操作に失敗しました: {exc}")
            return False
        self.dynstate.enabled = enabled
        self._dyn_apply_flags()
        dynconfig.save_state(DYNSTATE_PATH, self.dynstate)
        return True

    def _ark_dynconfig(self) -> None:
        """dynamic config(無停止で倍率変更)の設定ダイアログ。"""
        if not self.arkhosts:
            return
        dialog = tk.Toplevel(self)
        dialog.title("ARK dynamic config(無停止で倍率変更)")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("640x600+%d+%d"
                        % (self.winfo_rootx() + 120, self.winfo_rooty() + 40))

        head = ttk.Frame(dialog, padding=(10, 8, 10, 0))
        head.pack(fill=tk.X)
        ttk.Label(
            head, foreground=PAL["muted"], justify=tk.LEFT,
            text="倍率・イベント系だけを『サーバー再起動なし』で変更できる仕組み(全マップ共有)。\n"
                 "チェックした設定だけが上書きされます。ルール/構造/boolは対象外(⚙詳細設定＋再起動)。"
        ).pack(anchor=tk.W)

        # マスターON/OFF
        master = ttk.Frame(dialog, padding=(10, 6))
        master.pack(fill=tk.X)
        enabled_var = tk.BooleanVar(value=self.dynstate.enabled)
        url_var = tk.StringVar(value=self.dynserver.url)
        status_var = tk.StringVar()

        def refresh_status():
            run = "🟢 配信中" if self.dynserver.running else "⚪ 停止"
            # 稼働マップのうち -UseDynamicConfig 付きで動いているか(要再起動判定)
            status_var.set(f"HTTP: {run}   URL: {self.dynserver.url}")

        def on_toggle():
            want = enabled_var.get()
            if want:
                if not messagebox.askyesno(
                        "dynamic config を有効化",
                        "有効にすると:\n"
                        "・ローカルHTTP配信を開始(127.0.0.1)\n"
                        "・各マップの起動引数に -CustomDynamicConfigUrl と -UseDynamicConfig を付与\n\n"
                        "※ 今動いているサーバーは一度再起動して初めて有効になります。\n"
                        "　 以降は『⚡保存して即適用』で無停止反映できます。\n"
                        "続行しますか?", parent=dialog):
                    enabled_var.set(False)
                    return
            ok = self._dyn_set_enabled(want)
            if not ok:
                enabled_var.set(self.dynstate.enabled)
            refresh_status()

        ttk.Checkbutton(master, text="dynamic config を有効にする(マスター ON/OFF)",
                        variable=enabled_var, command=on_toggle).pack(side=tk.LEFT)
        ttk.Label(master, textvariable=status_var, foreground=PAL["muted"]).pack(
            side=tk.RIGHT)
        refresh_status()

        # 設定行(チェック=上書きON + 値)。スクロール枠を作る
        outer = ttk.Frame(dialog)
        outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        canvas = tk.Canvas(outer, highlightthickness=0, bg=PAL["bg"])
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        ttk.Label(inner, text="上書き", foreground=PAL["muted"]).grid(
            row=0, column=0, padx=6)
        ttk.Label(inner, text="設定", foreground=PAL["muted"]).grid(
            row=0, column=1, sticky=tk.W)
        ttk.Label(inner, text="値", foreground=PAL["muted"]).grid(row=0, column=2)
        rows = []
        for i, (key, kind, label, default) in enumerate(dynconfig.DYN_SETTINGS, start=1):
            active = key in self.dynstate.values
            av = tk.BooleanVar(value=active)
            ttk.Checkbutton(inner, variable=av).grid(row=i, column=0, padx=6)
            ttk.Label(inner, text=label, anchor=tk.W, wraplength=340).grid(
                row=i, column=1, sticky=tk.W, pady=2)
            vv = tk.StringVar(value=self.dynstate.values.get(key, default))
            ttk.Entry(inner, textvariable=vv, width=10).grid(row=i, column=2, padx=6)
            rows.append((key, kind, av, vv))
        inner.columnconfigure(1, weight=1)

        def collect():
            vals = {}
            for key, kind, av, vv in rows:
                if not av.get():
                    continue
                v = vv.get().strip()
                if v == "":
                    continue
                try:
                    float(v)
                except ValueError:
                    messagebox.showerror("入力エラー",
                                         f"「{key}」は数値で入力してください: {v}",
                                         parent=dialog)
                    return None
                vals[key] = v
            return vals

        def do_save(apply_now):
            vals = collect()
            if vals is None:
                return
            self.dynstate.values = vals
            try:
                dynconfig.write_file(DYNFILE_PATH, vals)
                dynconfig.save_state(DYNSTATE_PATH, self.dynstate)
            except Exception as exc:
                messagebox.showerror("保存エラー", str(exc), parent=dialog)
                return
            canvas.unbind_all("<MouseWheel>")
            dialog.destroy()
            if apply_now and self.dynstate.enabled:
                def job():
                    done = []
                    for ah in self.arkhosts:
                        try:
                            if ah.is_running():
                                ah.rcon_command("ForceUpdateDynamicConfig")
                                done.append(ah.cfg.display_name)
                        except Exception:
                            pass
                    return done

                def on_done(done, error):
                    if error is not None:
                        self._set_status(f"即適用エラー: {error}")
                        return
                    if done:
                        self._set_status("dynamic設定を即適用: " + ", ".join(done))
                    else:
                        self._set_status(
                            "保存しました(稼働中で-UseDynamicConfig付きのマップが無く即適用先なし)")
                self._task_submit("dynamic設定を即適用(ForceUpdate)", job, on_done,
                                  category="dynamic適用", busy=False)
            else:
                self._set_status("dynamic設定を保存しました(次の自動保存で反映)")

        btns = ttk.Frame(dialog, padding=(10, 8))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="URLコピー",
                   command=lambda: (self.clipboard_clear(),
                                    self.clipboard_append(self.dynserver.url),
                                    self._set_status("dynamic config URLをコピーしました"))
                   ).pack(side=tk.LEFT)
        ttk.Button(btns, text="閉じる",
                   command=lambda: (canvas.unbind_all("<MouseWheel>"),
                                    dialog.destroy())).pack(side=tk.RIGHT)
        ttk.Button(btns, text="⚡ 保存して即適用",
                   command=lambda: do_save(True)).pack(side=tk.RIGHT, padx=6)
        ttk.Button(btns, text="💾 保存のみ",
                   command=lambda: do_save(False)).pack(side=tk.RIGHT)

    def _sv_backup_open(self):
        server = self._selected_server()
        if server is None:
            return
        p = server.profile
        if p.game == "palworld":
            self._open_backup_dialog(
                f"バックアップ / 復元: {p.display_name}", p.name,
                backup_job=lambda pg: backup.pal_backup(p, self.backupcfg, progress=pg),
                restore_job=lambda bf, pg: backup.pal_restore(p, bf, progress=pg),
                stop_note="※ 復元前にこのサーバーを停止してください(セーブPal/Savedを置換します)。")
            return
        if p.game != "minecraft":
            messagebox.showinfo(
                "未対応", "この画面のバックアップはMinecraft/Palworld用です。ARKは🦖 ARKタブから。")
            return
        self._open_backup_dialog(
            f"バックアップ / 復元: {p.display_name}", p.name,
            backup_job=lambda pg: backup.mc_backup(p, self.backupcfg, progress=pg),
            restore_job=lambda bf, pg: backup.mc_restore(p, bf, progress=pg),
            stop_note="※ 復元前にこのサーバーを停止してください(ワールドを置換します)。")

    def _sv_update(self):
        """Palworld専用サーバーの更新(SteamCMD, VM上)。停止→更新→起動。"""
        server = self._selected_server()
        if server is None:
            return
        p = server.profile
        if p.game != "palworld":
            messagebox.showinfo(
                "未対応", "サーバー更新はPalworld専用サーバー用です。\n"
                "ARKは🦖 ARKタブ、Minecraftはjar手動更新です。")
            return
        self._begin_busy(f"{p.display_name}: 更新確認中…")

        def check_done(info, error):
            self._end_busy()
            if error is not None:
                messagebox.showerror("更新確認エラー", str(error))
                return
            if not info["update_available"]:
                messagebox.showinfo("最新です",
                                    f"{p.display_name} は最新です(build {info['installed']})")
                return
            if not messagebox.askyesno(
                    "サーバー更新",
                    f"{p.display_name} を更新しますか?\n"
                    f"build {info['installed']} → {info['latest']}\n\n"
                    "停止→更新→起動します。接続中のプレイヤーは切断されます。",
                    icon="warning", default="no"):
                return
            self._mark_restart(f"mc:{p.name}")

            def job():
                return palupdate.update(p, progress=self._progress_from_worker)

            def on_done(newb, err):
                if err is not None:
                    self._set_status(f"Palworld更新失敗: {err}")
                    self._notify("update", f"❌ {p.display_name} 更新失敗: {err}")
                    messagebox.showerror("更新失敗", str(err))
                    return
                self._set_status(f"{p.display_name} を更新しました(build {newb})")
                self._notify("update", f"✅ {p.display_name} を更新(build {newb})")

            self._task_submit(f"⬆ {p.display_name} 更新(SteamCMD)", job, on_done,
                              category="サーバー更新", busy=True)

        self._submit(lambda: palupdate.check(p), check_done)

    # ---------- 再起動予約(定期自動再起動スケジューラ) ----------

    def _build_sched_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent, foreground=PAL["muted"],
            text="↳ 指定した時刻(毎日)に自動で再起動します。プレイヤーが居れば60/30/10秒前にチャット予告。"
                 "設定は保存され、アプリを再起動しても復元されます。"
        ).pack(anchor=tk.W, padx=10, pady=(8, 2))

        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.sched_tree = ttk.Treeview(
            frame, columns=("kind", "action", "days", "times", "enabled"),
            show="tree headings", height=12, selectmode="browse")
        self.sched_tree.heading("#0", text="対象サーバー")
        self.sched_tree.heading("kind", text="種別")
        self.sched_tree.heading("action", text="動作")
        self.sched_tree.heading("days", text="曜日")
        self.sched_tree.heading("times", text="時刻")
        self.sched_tree.heading("enabled", text="有効(クリックで切替)")
        self.sched_tree.column("#0", width=210)
        self.sched_tree.column("kind", width=64, anchor=tk.CENTER)
        self.sched_tree.column("action", width=110, anchor=tk.CENTER)
        self.sched_tree.column("days", width=96, anchor=tk.CENTER)
        self.sched_tree.column("times", width=150, anchor=tk.CENTER)
        self.sched_tree.column("enabled", width=120, anchor=tk.CENTER)
        self.sched_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.sched_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.sched_tree.configure(yscrollcommand=sb.set)
        self.sched_tree.bind("<Double-1>", self._sched_on_double)
        self.sched_tree.bind("<Button-1>", self._sched_on_click)

        btns = ttk.Frame(parent)
        btns.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Button(btns, text="＋ 追加", command=lambda: self._sched_edit(None)).pack(side=tk.LEFT)
        ttk.Button(btns, text="✎ 編集", command=self._sched_edit).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="⏯ 有効/無効", command=self._sched_toggle).pack(side=tk.LEFT)
        ttk.Button(btns, text="🗑 削除", command=self._sched_delete).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="▶ 今すぐ実行(テスト)",
                   command=self._sched_run_now).pack(side=tk.RIGHT)
        self._sched_refresh_tree()

    def _sched_targets(self) -> list[tuple[str, str, str, str]]:
        """選択肢: (表示ラベル, kind, target, display)。ARKマップ + MCサーバー。"""
        out = []
        for ah in self.arkhosts:
            out.append((f"🦖 {ah.cfg.display_name}", "ark", ah.cfg.map_label,
                        ah.cfg.display_name))
        for name, srv in self.servers.items():
            if srv.profile.game == "minecraft":
                out.append((f"🟩 {srv.profile.display_name}", "mc", name,
                            srv.profile.display_name))
            elif srv.profile.game == "palworld":  # 予約対象にPalworldも(再起動=systemd)
                out.append((f"🐑 {srv.profile.display_name}", "mc", name,
                            srv.profile.display_name))
        return out

    def _sched_refresh_tree(self) -> None:
        self.sched_tree.delete(*self.sched_tree.get_children())
        for job in self.schedules:
            kind = "🦖ARK" if job.kind == "ark" else "🟩MC"
            act = "💾バックアップ" if job.action == "backup" else "🔁再起動"
            name = job.display
            self.sched_tree.insert(
                "", tk.END, iid=job.id, text=name,
                values=(kind, act, job.days_text(), job.times_text(),
                        "✅ 有効" if job.enabled else "⏸ 無効"))

    def _sched_on_click(self, event) -> str | None:
        """有効列をクリックしたらその場で ON/OFF を切り替える。"""
        if self.sched_tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.sched_tree.identify_column(event.x) != "#5":   # enabled列
            return None
        row = self.sched_tree.identify_row(event.y)
        job = next((j for j in self.schedules if j.id == row), None)
        if job is not None:
            job.enabled = not job.enabled
            self._sched_save()
            self._sched_refresh_tree()
            self.sched_tree.selection_set(row)
        return "break"

    def _sched_on_double(self, event) -> str | None:
        if self.sched_tree.identify_column(event.x) == "#5":   # 有効列は切替専用
            return "break"
        self._sched_edit()
        return None

    def _sched_selected(self):
        sel = self.sched_tree.selection()
        if not sel:
            return None
        return next((j for j in self.schedules if j.id == sel[0]), None)

    def _sched_edit(self, job="__sel__") -> None:
        if job == "__sel__":
            job = self._sched_selected()
            if job is None:
                messagebox.showinfo("選択なし", "編集する予約を選んでください")
                return
        targets = self._sched_targets()
        if not targets:
            messagebox.showinfo("対象なし", "予約できるサーバーがありません")
            return

        dialog = tk.Toplevel(self)
        dialog.title("再起動予約の編集" if job else "再起動予約の追加")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("470x400+%d+%d"
                        % (self.winfo_rootx() + 200, self.winfo_rooty() + 80))
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill=tk.BOTH, expand=True)

        ttk.Label(form, text="対象サーバー:").grid(row=0, column=0, sticky=tk.W, pady=4)
        labels = [t[0] for t in targets]
        tgt_var = tk.StringVar()
        combo = ttk.Combobox(form, textvariable=tgt_var, values=labels,
                             state="readonly", width=36)
        combo.grid(row=0, column=1, sticky=tk.W, pady=4)
        # 既存ジョブなら現在の対象を選択
        cur_idx = 0
        if job is not None:
            for i, t in enumerate(targets):
                if t[1] == job.kind and t[2] == job.target:
                    cur_idx = i
                    break
        combo.current(cur_idx)

        ttk.Label(form, text="再起動時刻:").grid(row=1, column=0, sticky=tk.W, pady=4)
        times_var = tk.StringVar(value=", ".join(job.times) if job else "04:00")
        ttk.Entry(form, textvariable=times_var, width=36).grid(
            row=1, column=1, sticky=tk.W, pady=4)
        ttk.Label(form, foreground=PAL["muted"],
                  text="HH:MM をカンマ区切りで複数可(例: 04:00, 16:00)").grid(
            row=2, column=1, sticky=tk.W)

        ttk.Label(form, text="曜日:").grid(row=3, column=0, sticky=tk.W, pady=(8, 2))
        days_frame = ttk.Frame(form)
        days_frame.grid(row=3, column=1, sticky=tk.W, pady=(8, 2))
        day_vars = []
        cur_days = set(job.days) if job else set()
        for i, lbl in enumerate(scheduler.WEEKDAY_LABELS):
            dv = tk.BooleanVar(value=(i in cur_days))
            ttk.Checkbutton(days_frame, text=lbl, variable=dv).pack(side=tk.LEFT)
            day_vars.append(dv)
        ttk.Label(form, foreground=PAL["muted"],
                  text="何も選ばない=毎日").grid(row=4, column=1, sticky=tk.W)

        ttk.Label(form, text="動作:").grid(row=5, column=0, sticky=tk.W, pady=(8, 2))
        action_var = tk.StringVar(
            value="バックアップ" if (job and job.action == "backup") else "再起動")
        ttk.Combobox(form, textvariable=action_var, state="readonly", width=16,
                     values=["再起動", "バックアップ"]).grid(
            row=5, column=1, sticky=tk.W, pady=(8, 2))

        ttk.Label(form, foreground=PAL["muted"],
                  text="🦕 ARK再起動時の恐竜リスポーンは ⚙詳細設定 のトグルで一括設定").grid(
            row=6, column=1, sticky=tk.W, pady=(6, 0))
        enabled_var = tk.BooleanVar(value=job.enabled if job else True)
        ttk.Checkbutton(form, text="有効", variable=enabled_var).grid(
            row=7, column=1, sticky=tk.W, pady=6)

        def ok() -> None:
            raw = [scheduler.normalize_time(t) for t in times_var.get().split(",") if t.strip()]
            times = [t for t in raw if t]
            if not times:
                messagebox.showerror("入力エラー",
                                     "時刻を HH:MM 形式で1つ以上入力してください(例 04:00)",
                                     parent=dialog)
                return
            days = [i for i, dv in enumerate(day_vars) if dv.get()]  # 空=毎日
            action = "backup" if action_var.get() == "バックアップ" else "restart"
            tgt = targets[labels.index(tgt_var.get())]
            if job is None:
                new = scheduler.RestartJob(
                    id=uuid.uuid4().hex[:8], kind=tgt[1], target=tgt[2],
                    display=tgt[3], times=times, days=days, enabled=enabled_var.get(),
                    action=action)
                self.schedules.append(new)
            else:
                job.kind, job.target, job.display = tgt[1], tgt[2], tgt[3]
                job.times, job.days, job.enabled = times, days, enabled_var.get()
                job.action = action
            self._sched_save()
            self._sched_refresh_tree()
            dialog.destroy()

        bar = ttk.Frame(form)
        bar.grid(row=8, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(bar, text="保存", command=ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT)

    def _sched_toggle(self) -> None:
        job = self._sched_selected()
        if job is None:
            return
        job.enabled = not job.enabled
        self._sched_save()
        self._sched_refresh_tree()

    def _sched_delete(self) -> None:
        job = self._sched_selected()
        if job is None:
            return
        if not messagebox.askyesno("確認", f"予約「{job.display}」を削除しますか?"):
            return
        self.schedules = [j for j in self.schedules if j.id != job.id]
        self._sched_save()
        self._sched_refresh_tree()

    def _sched_run_now(self) -> None:
        job = self._sched_selected()
        if job is None:
            messagebox.showinfo("選択なし", "テスト実行する予約を選んでください")
            return
        note = ("(プレイヤーが居れば60秒予告してから再起動します)"
                if job.action != "backup" else "(バックアップを今すぐ実行します)")
        if not messagebox.askyesno(
                "確認", f"「{job.display}」の{job.action_text()}を今すぐ実行しますか?\n{note}",
                icon="warning", default="no"):
            return
        self._sched_fire(job)

    def _sched_save(self) -> None:
        try:
            scheduler.save_jobs(SCHEDULES_PATH, self.schedules)
        except Exception as exc:
            messagebox.showerror("保存エラー", f"予約の保存に失敗: {exc}")

    def _sched_tick(self) -> None:
        now = datetime.now()
        for job in scheduler.due_jobs(self.schedules, now):
            key = (job.id, now.strftime("%H:%M"), now.strftime("%Y-%m-%d"))
            if key in self._sched_fired:
                continue
            self._sched_fired.add(key)
            self._sched_fire(job)
        today = now.strftime("%Y-%m-%d")   # 当日分だけ保持(メモリ肥大防止)
        self._sched_fired = {k for k in self._sched_fired if k[2] == today}
        self.after(SCHED_TICK_MS, self._sched_tick)

    def _sched_fire(self, job) -> None:
        """予約の発火: 再起動 or バックアップ。停止中はスキップ。タスク画面に記録。"""
        if job.action == "backup":
            self._sched_fire_backup(job)
            return
        if job.kind == "ark":
            ah = next((a for a in self.arkhosts if a.cfg.map_label == job.target), None)
            if ah is None:
                self._append_log(f"⏰ 予約再起動: ARK '{job.target}' が見つからずスキップ")
                return
            respawn = self.ark_respawn_on_restart   # ⚙詳細設定のトグルで決まる
            self._mark_restart(f"ark:{self.arkhosts.index(ah)}")

            def job_fn():
                if not ah.is_running():
                    self._progress_from_worker("停止中のため予約再起動をスキップ")
                    return "skipped"
                ah.restart_with_notice(respawn_dinos=respawn,
                                       progress=self._progress_from_worker)
                return "done"
            title = f"⏰ 予約再起動: {ah.cfg.display_name}" + ("+恐竜リスポーン" if respawn else "")
            self._task_submit(
                title, job_fn,
                self._ark_action_done(f"⏰ {ah.cfg.display_name} を予約再起動しました", 4000),
                category="予約再起動", busy=False)
        else:
            server = self.servers.get(job.target)
            if server is None:
                self._append_log(f"⏰ 予約再起動: MC '{job.target}' が見つからずスキップ")
                return
            self._mark_restart(f"mc:{job.target}")

            def job_fn():
                return self._mc_restart_with_notice(server, self._progress_from_worker)
            self._task_submit(
                f"⏰ 予約再起動: {server.profile.display_name}", job_fn,
                self._make_action_done(f"⏰ {server.profile.display_name} を予約再起動しました"),
                category="予約再起動", busy=False)

    def _sched_fire_backup(self, job) -> None:
        """予約バックアップの発火(ARK=マップ別zip / MC=tar.gz)。"""
        if job.kind == "ark":
            ah = next((a for a in self.arkhosts if a.cfg.map_label == job.target), None)
            if ah is None:
                self._append_log(f"⏰ 予約バックアップ: ARK '{job.target}' が見つからずスキップ")
                return
            saved_root = str(backup.ark_saved_dir(ah.cfg.config_dir))
            label, subdir = ah.cfg.map_label, ah.cfg.save_subdir

            def job_fn():
                return backup.ark_backup(saved_root, self.backupcfg, label, subdir,
                                         progress=self._progress_from_worker)
            disp = ah.cfg.display_name
        else:
            server = self.servers.get(job.target)
            if server is None:
                self._append_log(f"⏰ 予約バックアップ: MC '{job.target}' が見つからずスキップ")
                return
            p = server.profile
            # 予約対象では Palworld も kind="mc" で登録されるため、ゲームで分岐する。
            if p.game == "palworld":
                def job_fn():
                    return backup.pal_backup(p, self.backupcfg,
                                             progress=self._progress_from_worker)
            else:
                def job_fn():
                    return backup.mc_backup(p, self.backupcfg,
                                            progress=self._progress_from_worker)
            disp = p.display_name

        def on_done(path, error):
            if error is not None:
                self._set_status(f"予約バックアップ失敗: {disp} ({error})")
                self._notify("backup", f"❌ {disp} の予約バックアップに失敗: {error}")
            else:
                name = Path(path).name if path else "?"
                self._set_status(f"⏰ {disp} をバックアップしました: {name}")
                self._notify("backup", f"💾 {disp} を自動バックアップしました\n{name}")
        self._task_submit(f"⏰ 予約バックアップ: {disp}", job_fn, on_done,
                          category="予約バックアップ", busy=False)

    def _pal_player_count(self, server) -> int:
        try:
            raw = server.rcon_command("ShowPlayers")
        except Exception:
            return 0
        lines = [l for l in (raw or "").splitlines() if l.strip()]
        return max(0, len(lines) - 1)   # 先頭はヘッダ

    def _mc_restart_with_notice(self, server, progress) -> str:
        """MC/Palworldを予告付きで再起動。停止中ならスキップ。"""
        try:
            st = server.status()
        except Exception:
            st = "unknown"
        if st != "active":
            progress(f"稼働していない({st})ため予約再起動をスキップ")
            return "skipped"
        if server.profile.game == "palworld":    # Palworld: Shutdownで予告→起動
            n = self._pal_player_count(server)
            if n > 0:
                progress("Shutdown 30 で予告(ゲーム内表示)…")
                try:
                    server.rcon_command("Shutdown 30 Server_restarting_in_30s")
                except Exception:
                    pass
                for _ in range(20):
                    time.sleep(3)
                    if server.status() != "active":
                        break
                server.start()                    # 落ちていれば起動
            else:
                progress("プレイヤー不在のため即再起動します")
                server.restart()
            return "done"
        # プレイヤーが居ればsayで予告
        n = 0
        try:
            import re
            raw = server.rcon_command("list")
            m = re.search(r'There are (\d+)', raw or "")
            n = int(m.group(1)) if m else 0
        except Exception:
            n = 0
        if n > 0:
            for secs, nxt, msg in [
                    (60, 30, "[GSM] Server will RESTART in 60 seconds. Please log off safely."),
                    (30, 10, "[GSM] Restart in 30 seconds..."),
                    (10, 0, "[GSM] Restart in 10 seconds!")]:
                progress(f"予告(残り{secs}秒): {msg}")
                try:
                    server.rcon_command(f"say {msg}")
                except Exception:
                    pass
                time.sleep(secs - nxt)
            try:
                server.rcon_command("say [GSM] Restarting now. Back in ~1 minute.")
            except Exception:
                pass
        else:
            progress("プレイヤー不在のため予告を省略して再起動します")
        server.restart()
        return "done"

    # ---------- タスク画面(操作の記録・可視化) ----------

    def _build_task_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent, foreground=PAL["muted"],
            text="↳ 起動・停止・設定変更・バックアップなどの操作履歴。行をクリックで実行ステップ(ロジック)と結果を表示"
        ).pack(anchor=tk.W, padx=10, pady=(8, 2))

        paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # 左: タスク一覧
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.task_tree = ttk.Treeview(
            left, columns=("status", "cat", "time", "elapsed"),
            show="tree headings", height=14, selectmode="browse")
        self.task_tree.heading("#0", text="操作")
        self.task_tree.heading("status", text="結果")
        self.task_tree.heading("cat", text="種別")
        self.task_tree.heading("time", text="開始")
        self.task_tree.heading("elapsed", text="所要")
        self.task_tree.column("#0", width=300)
        self.task_tree.column("status", width=90, anchor=tk.CENTER)
        self.task_tree.column("cat", width=90, anchor=tk.CENTER)
        self.task_tree.column("time", width=80, anchor=tk.CENTER)
        self.task_tree.column("elapsed", width=70, anchor=tk.CENTER)
        self.task_tree.tag_configure("failed", foreground=PAL["error"])
        self.task_tree.tag_configure("success", foreground=PAL["ok"])
        self.task_tree.tag_configure("running", foreground=PAL["accent"])
        self.task_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.task_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.task_tree.configure(yscrollcommand=sb.set)
        self.task_tree.bind("<<TreeviewSelect>>", self._task_on_select)

        # 右: 詳細(実行ステップ・結果)
        right = ttk.LabelFrame(paned, text="詳細(実行ステップ)")
        paned.add(right, weight=2)
        self.task_detail = tk.Text(
            right, height=14, width=44, wrap=tk.WORD, state=tk.DISABLED,
            bg=PAL["surface"], fg=PAL["text"], relief=tk.FLAT,
            font=("Consolas", 9), padx=8, pady=8)
        self.task_detail.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        btns = ttk.Frame(parent)
        btns.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Button(btns, text="🗑 履歴をクリア", command=self._task_clear)\
            .pack(side=tk.RIGHT)
        # 既存タスク(起動直後は空)を反映
        for t in reversed(self._tasks):
            self._task_tree_add(t)

    def _task_row_values(self, task: "_Task"):
        return (task.status_label, task.category,
                task.started.strftime("%H:%M:%S"), task.elapsed)

    def _task_tree_add(self, task: "_Task") -> None:
        if not hasattr(self, "task_tree"):
            return
        self.task_tree.insert(
            "", 0, iid=str(task.id), text=task.title,
            values=self._task_row_values(task), tags=(task.status,))

    def _task_tree_update(self, task: "_Task") -> None:
        if not hasattr(self, "task_tree") or not self.task_tree.exists(str(task.id)):
            return
        self.task_tree.item(str(task.id), values=self._task_row_values(task),
                            tags=(task.status,))

    def _task_on_select(self, _event=None) -> None:
        sel = self.task_tree.selection()
        if not sel:
            return
        self._task_selected_id = int(sel[0])
        task = next((t for t in self._tasks if t.id == self._task_selected_id), None)
        if task is not None:
            self._task_show_detail(task)

    def _task_show_detail(self, task: "_Task") -> None:
        if not hasattr(self, "task_detail"):
            return
        lines = [
            f"■ {task.title}",
            f"種別: {task.category}",
            f"結果: {task.status_label}",
            f"開始: {task.started.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if task.ended:
            lines.append(f"終了: {task.ended.strftime('%Y-%m-%d %H:%M:%S')}  (所要 {task.elapsed})")
        if task.error:
            lines.append(f"エラー: {task.error}")
        lines.append("")
        lines.append("── 実行ステップ ──")
        lines.extend(task.log)
        self.task_detail.configure(state=tk.NORMAL)
        self.task_detail.delete("1.0", tk.END)
        self.task_detail.insert(tk.END, "\n".join(lines))
        self.task_detail.configure(state=tk.DISABLED)

    def _task_clear(self) -> None:
        # 実行中タスクは残す(誤って進行中の記録を消さない)
        keep = [t for t in self._tasks if t.status == "running"]
        self._tasks = keep
        for iid in self.task_tree.get_children():
            self.task_tree.delete(iid)
        for t in reversed(keep):
            self._task_tree_add(t)
        self._task_selected_id = None
        self.task_detail.configure(state=tk.NORMAL)
        self.task_detail.delete("1.0", tk.END)
        self.task_detail.configure(state=tk.DISABLED)

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        # --- 自動ポート開放(config.yamlとは別管理) ---
        pf = ttk.LabelFrame(parent, text="🔌 自動ポート開放(UPnP)")
        pf.pack(fill=tk.X, padx=10, pady=(10, 4))
        self.autoport_var = tk.BooleanVar(value=self.autoport_enabled)
        ttk.Checkbutton(
            pf, variable=self.autoport_var,
            text="サーバー起動中だけポートを開ける(停止したら閉じる/常時開放しない)",
            command=lambda: self._portsync_set_enabled(self.autoport_var.get())
        ).pack(anchor=tk.W, padx=8, pady=(6, 2))
        ttk.Label(
            pf, foreground=PAL["muted"], justify=tk.LEFT,
            text="MC=TCP(VMへ転送)/ ARK=UDP ゲーム+クエリ(ホストへ転送)。約30秒ごとに状態と照合。\n"
                 "GSMが開けた分(gsm-auto)だけ開閉し、手動公開や他機器のマッピングには触りません。"
        ).pack(anchor=tk.W, padx=8, pady=(0, 6))

        # --- クラッシュ自動復旧 ---
        cf = ttk.LabelFrame(parent, text="🔧 クラッシュ自動復旧")
        cf.pack(fill=tk.X, padx=10, pady=4)
        self.crash_var = tk.BooleanVar(value=self.crash_recovery)
        ttk.Checkbutton(
            cf, variable=self.crash_var,
            text="サーバーが予期せず落ちたら自動で再起動する",
            command=lambda: self._crash_set_enabled(self.crash_var.get())
        ).pack(anchor=tk.W, padx=8, pady=(6, 2))
        ttk.Label(cf, foreground=PAL["muted"], justify=tk.LEFT,
                  text="GSMからの停止/再起動は復旧対象外。連続クラッシュ時は120秒のクールダウンあり。"
                  ).pack(anchor=tk.W, padx=8, pady=(0, 6))

        # --- Discord通知 ---
        self._build_notify_section(parent)

        try:
            raw = settings.read_raw(CONFIG_PATH)
        except Exception as exc:
            ttk.Label(parent, text=f"config.yamlの読み込みに失敗: {exc}", padding=20).pack()
            return

        def get(*keys, default: str = "") -> str:
            node = raw
            for k in keys:
                if not isinstance(node, dict) or node.get(k) is None:
                    return default
                node = node[k]
            return str(node)

        sections = [
            ("ネットワーク(VM用)", [
                (("network", "subnet"), "サブネット",
                 get("network", "subnet", default="192.168.11.0/24"), False),
                (("network", "vm_range"), "VM用第4オクテット範囲(例 100-199)",
                 get("network", "vm_range", default="100-199"), False),
                (("network", "gateway"), "ゲートウェイ",
                 get("network", "gateway", default="192.168.11.1"), False),
            ]),
            ("DNS自動登録(ipamのPowerDNS)", [
                (("dns", "host"), "DNSホスト", get("dns", "host"), False),
                (("dns", "domain"), "ドメイン", get("dns", "domain"), False),
                (("dns", "ssh", "user"), "SSHユーザー", get("dns", "ssh", "user"), False),
                (("dns", "ssh", "password"), "SSHパスワード",
                 get("dns", "ssh", "password"), True),
            ]),
            ("SQL共有(MySQL)", [
                (("mysql", "host"), "ホスト", get("mysql", "host"), False),
                (("mysql", "port"), "ポート", get("mysql", "port", default="3306"), False),
                (("mysql", "user"), "ユーザー", get("mysql", "user"), False),
                (("mysql", "password"), "パスワード", get("mysql", "password"), True),
                (("mysql", "prefix"), "DBプレフィックス",
                 get("mysql", "prefix", default="gsdata_"), False),
            ]),
        ]

        self._settings_fields: list[tuple[tuple, tk.StringVar]] = []
        wrap = ttk.Frame(parent, padding=8)
        wrap.pack(fill=tk.BOTH, expand=True)
        for col, (title, fields) in enumerate(sections):
            frame = ttk.LabelFrame(wrap, text=title, padding=8)
            frame.grid(row=0, column=col, sticky=tk.NSEW, padx=4, pady=4)
            for i, (path, label, value, secret) in enumerate(fields):
                ttk.Label(frame, text=label).grid(row=i, column=0, sticky=tk.W, pady=3)
                var = tk.StringVar(value=value)
                ttk.Entry(frame, textvariable=var, width=22,
                          show="*" if secret else "").grid(
                    row=i, column=1, sticky=tk.W, padx=(6, 0), pady=3)
                self._settings_fields.append((path, var))

        note = ttk.Label(
            wrap, foreground="#777777",
            text="※ ゲームサーバーのプロファイルは「⚙ 新規サーバー構築」で自動追加されます。\n"
                 "   保存すると config.yaml に書き込まれ(コメント保持)、アプリに即反映されます。")
        note.grid(row=1, column=0, columnspan=3, sticky=tk.W, padx=4, pady=(10, 4))
        ttk.Button(wrap, text="💾 設定を保存", command=self._save_settings).grid(
            row=2, column=0, sticky=tk.W, padx=4, pady=4)

        # --- 外部公開ヘルス ---
        if self.config_data.publish is not None:
            pub_frame = ttk.LabelFrame(wrap, text="外部公開ヘルス(FQDNで外部から到達できるか)",
                                       padding=8)
            pub_frame.grid(row=3, column=0, columnspan=3, sticky=tk.EW, padx=4, pady=(8, 4))
            self.publish_status_var = tk.StringVar(value="未確認")
            self.publish_status_lbl = ttk.Label(pub_frame, textvariable=self.publish_status_var,
                                                 font=("", 10, "bold"))
            self.publish_status_lbl.pack(anchor=tk.W)
            self.publish_detail = tk.Text(pub_frame, height=7, wrap=tk.WORD,
                                          state=tk.DISABLED, font=("", 9))
            self.publish_detail.pack(fill=tk.X, pady=(4, 4))
            row = ttk.Frame(pub_frame)
            row.pack(fill=tk.X)
            ttk.Button(row, text="🔎 今すぐ確認", command=self._publish_check).pack(side=tk.LEFT)
            ttk.Label(row, foreground="#777777",
                      text=f"  自動チェック: {PUBLISH_CHECK_MS // 60000}分ごと(問題時に通知)"
                      ).pack(side=tk.LEFT)

    def _publish_auto_check(self) -> None:
        self._publish_check(silent=True)
        self.after(PUBLISH_CHECK_MS, self._publish_auto_check)

    # ---------- 自動ポート開放(起動中だけ開ける) ----------

    def _portsync_gateway(self):
        """NATしているルーターをキャッシュ付きで取得(10分でリフレッシュ)。"""
        now = time.time()
        if self._portsync_gw is not None and now - self._portsync_gw_ts < 600:
            return self._portsync_gw
        gw = upnp.find_gateway(
            bind_ip=upnp.local_ip_toward(self.config_data.network.gateway),
            prefer_host=self.config_data.network.gateway)
        self._portsync_gw = gw
        self._portsync_gw_ts = now
        return gw

    def _portsync_specs(self) -> list:
        """開閉対象のポート一覧(MC=TCP/VM、ARK=UDP/ホスト)。desired=起動中か。

        ※ 状態が未取得(まだ一度も監視できていない)のサーバーは対象にしない。
        　 起動直後にキャッシュが空の状態で『全部停止中』とみなして誤って閉じるのを防ぐ。"""
        specs = []
        for name, srv in self.servers.items():
            p = srv.profile
            if not p.game_port or name not in self._server_running:
                continue                          # 状態未取得はスキップ
            proto = "UDP" if p.game == "palworld" else "TCP"   # PalworldはUDP
            specs.append(portsync.PortSpec(
                label=f"{p.game}-{name}", ext_port=(p.external_port or p.game_port),
                internal_ip=p.address, internal_port=p.game_port, proto=proto,
                desired=self._server_running[name]))
        if self.arkhosts:
            host_ip = upnp.local_ip_toward(self.config_data.network.gateway)
            for i, ah in enumerate(self.arkhosts):
                if i not in self._ark_running:
                    continue                      # 状態未取得はスキップ
                running = self._ark_running[i]
                if ah.cfg.game_port:
                    specs.append(portsync.PortSpec(
                        f"ark-{ah.cfg.map_label}-game", ah.cfg.game_port, host_ip,
                        ah.cfg.game_port, "UDP", running))
                if ah.cfg.query_port:
                    specs.append(portsync.PortSpec(
                        f"ark-{ah.cfg.map_label}-query", ah.cfg.query_port, host_ip,
                        ah.cfg.query_port, "UDP", running))
        return specs

    def _portsync_on_change(self) -> None:
        """サーバーの起動/停止を検知したら即座に開閉を反映する。"""
        if self.autoport_enabled:
            self._submit(self._portsync_job, self._portsync_done)

    def _portsync_tick(self) -> None:
        if self.autoport_enabled:                # 30秒ごとの保険(取りこぼし対策)
            self._submit(self._portsync_job, self._portsync_done)
        self.after(PORTSYNC_TICK_MS, self._portsync_tick)

    @staticmethod
    def _portsync_manageable(m, spec) -> bool:
        """このマッピングを管理対象とみなすか(GSMが開けた or 転送先が対象サーバー)。

        管理は『対象サーバーのポート』に限定(ext_port一致でループ)されるため、
        DNS(53)やRDP(3389)など無関係ポートには最初から触れない。転送先IP一致で
        既存の常時開放(手動公開など)も引き継いで開閉できる。"""
        if m is None:
            return False
        return portsync.is_ours(m) or str(m.get("internal_client", "")) == spec.internal_ip

    def _portsync_job(self) -> list:
        """ルーターの現状とサーバー状態を照合し、対象サーバーのポートを開閉する。"""
        if not self.autoport_enabled:
            return []
        gw = self._portsync_gateway()
        existing = {}
        for m in gw.client.list_port_mappings():
            existing[(str(m.get("external_port")),
                      (m.get("protocol") or "").upper())] = m
        actions = []
        for spec in self._portsync_specs():
            m = existing.get((str(spec.ext_port), spec.proto.upper()))
            mine = self._portsync_manageable(m, spec)
            if spec.desired:                     # 起動中 → 開いていること
                if m is None:
                    upnp.add_mapping(gw, spec.ext_port, spec.internal_ip,
                                     spec.internal_port, spec.proto, description=spec.desc)
                    actions.append(f"開 {spec.label} {spec.proto}/{spec.ext_port}")
                elif mine and not portsync.is_ours(m):   # 既存の常時開放を引き継ぐ
                    upnp.delete_mapping(gw, spec.ext_port, spec.proto)
                    upnp.add_mapping(gw, spec.ext_port, spec.internal_ip,
                                     spec.internal_port, spec.proto, description=spec.desc)
                    actions.append(f"引継 {spec.label} {spec.proto}/{spec.ext_port}")
            else:                                # 停止中 → 閉じていること
                if mine:
                    upnp.delete_mapping(gw, spec.ext_port, spec.proto)
                    actions.append(f"閉 {spec.label} {spec.proto}/{spec.ext_port}")
        return actions

    def _portsync_close_all(self) -> list:
        """対象サーバーの開放ポートを全て閉じる(無効化時。無関係ポートには触れない)。"""
        gw = self._portsync_gateway()
        existing = {}
        for m in gw.client.list_port_mappings():
            existing[(str(m.get("external_port")),
                      (m.get("protocol") or "").upper())] = m
        actions = []
        for spec in self._portsync_specs():
            m = existing.get((str(spec.ext_port), spec.proto.upper()))
            if self._portsync_manageable(m, spec):
                upnp.delete_mapping(gw, spec.ext_port, spec.proto)
                actions.append(f"閉 {spec.proto}/{spec.ext_port}")
        return actions

    def _portsync_done(self, actions, error) -> None:
        if error is not None:
            self._set_status_idle(f"自動ポート開放: ルーター応答なし ({error})")
            return
        if actions:
            self._set_status("自動ポート開放: " + " / ".join(actions))
            self._append_log("■ 自動ポート開放\n  " + "\n  ".join(actions))

    def _portsync_set_enabled(self, enabled: bool) -> None:
        self.autoport_enabled = enabled
        portsync.save_enabled(PORTSYNC_PATH, enabled)
        if enabled:
            self._set_status("自動ポート開放: 有効。サーバー状態を取得して開放します…")
            # まず状態を取り込み(未取得だと誤って閉じないようスキップされる)、少し待って照合
            self.refresh_all()
            if self.arkhosts:
                self._ark_refresh(silent=True)
            self.after(5000, lambda: self.autoport_enabled
                       and self._submit(self._portsync_job, self._portsync_done))
        else:
            self._set_status("自動ポート開放: 無効化。GSMが開けたポートを閉じます…")
            self._submit(self._portsync_close_all, self._portsync_done)

    # ---------- 通知(Discord)/ クラッシュ自動復旧 / 状態遷移 ----------

    def _notify(self, event: str, text: str) -> None:
        """Discordへ通知(該当イベントが有効なときだけ、ワーカーで送信)。"""
        if not self.notifycfg.wants(event):
            return
        url = self.notifycfg.webhook_url
        self._submit(lambda: notify.send(url, text), lambda _r, _e: None)

    def _mark_stop(self, key: str) -> None:
        self._op_stop[key] = time.time()

    def _mark_restart(self, key: str) -> None:
        self._op_restart[key] = time.time()

    @staticmethod
    def _recent(d: dict, key: str, window: float = 300) -> bool:
        return (time.time() - d.get(key, 0)) < window

    def _server_state_changed(self, key, kind, display, running, ref) -> None:
        """MC/ARKの起動・停止を検知したときの共通処理(ポート/通知/クラッシュ復旧)。"""
        self._portsync_on_change()
        if running:
            self._crash_cooldown.pop(key, None)
            if self._recent(self._op_restart, key):
                self._op_restart.pop(key, None)
                self._notify("restart", f"🔁 {display} を再起動しました")
            else:
                self._notify("server_up", f"🟢 {display} が起動しました")
        else:
            if self._recent(self._op_restart, key):
                pass                              # 再起動の一時停止 → 通知しない
            elif self._recent(self._op_stop, key):
                self._op_stop.pop(key, None)
                self._notify("server_down", f"⚪ {display} を停止しました")
            else:                                 # 予期せぬ停止 = クラッシュ
                self._notify("crash", f"⚠️ {display} が予期せず停止しました(クラッシュの可能性)")
                if self.crash_recovery and not self._recent(self._crash_cooldown, key, 120):
                    self._crash_cooldown[key] = time.time()
                    self._crash_autorestart(kind, display, ref)

    def _crash_autorestart(self, kind, display, ref) -> None:
        def job():
            if kind == "ark":
                if not ref.is_running():
                    ref.start(progress=self._progress_from_worker)
            else:
                start_server_with_vm(self.hyperv, ref, progress=self._progress_from_worker)
            return True

        def on_done(_r, error):
            if error is not None:
                self._notify("crash", f"❌ {display} の自動復旧に失敗しました: {error}")
                self._set_status(f"自動復旧失敗: {display}")
            else:
                self._notify("crash", f"✅ {display} を自動復旧しました")
                self._set_status(f"自動復旧: {display} を再起動しました")

        self._set_status(f"クラッシュ検知 → {display} を自動復旧中…")
        self._task_submit(f"🔧 自動復旧: {display}", job, on_done,
                          category="自動復旧", busy=False)

    def _build_notify_section(self, parent) -> None:
        nf = ttk.LabelFrame(parent, text="🔔 Discord通知")
        nf.pack(fill=tk.X, padx=10, pady=4)
        self.notify_enabled_var = tk.BooleanVar(value=self.notifycfg.enabled)
        ttk.Checkbutton(nf, text="Discordへ通知する", variable=self.notify_enabled_var
                        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(6, 2))
        ttk.Label(nf, text="Webhook URL:").grid(row=1, column=0, sticky=tk.W, padx=8)
        self.notify_url_var = tk.StringVar(value=self.notifycfg.webhook_url)
        ttk.Entry(nf, textvariable=self.notify_url_var, width=52).grid(
            row=1, column=1, sticky=tk.W, padx=6, pady=2)
        # 通知イベントのチェック
        ev_frame = ttk.Frame(nf)
        ev_frame.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=8, pady=2)
        self.notify_event_vars = {}
        for i, (key, label) in enumerate(notify.EVENT_LABELS.items()):
            v = tk.BooleanVar(value=self.notifycfg.events.get(key, False))
            self.notify_event_vars[key] = v
            ttk.Checkbutton(ev_frame, text=label, variable=v).grid(
                row=i // 3, column=i % 3, sticky=tk.W, padx=(0, 12))
        bar = ttk.Frame(nf)
        bar.grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(4, 8))
        ttk.Button(bar, text="保存", command=self._notify_save).pack(side=tk.LEFT)
        ttk.Button(bar, text="テスト送信", command=self._notify_test).pack(side=tk.LEFT, padx=6)
        ttk.Label(nf, foreground=PAL["muted"],
                  text="Discordのチャンネル設定→連携→Webhook でURLを発行して貼り付け。"
                  ).grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(0, 6))

    def _notify_collect(self) -> None:
        self.notifycfg.enabled = self.notify_enabled_var.get()
        self.notifycfg.webhook_url = self.notify_url_var.get().strip()
        for key, v in self.notify_event_vars.items():
            self.notifycfg.events[key] = v.get()

    def _notify_save(self) -> None:
        self._notify_collect()
        try:
            notify.save(NOTIFY_PATH, self.notifycfg)
            self._set_status("Discord通知の設定を保存しました")
        except Exception as exc:
            messagebox.showerror("保存エラー", str(exc))

    def _notify_test(self) -> None:
        self._notify_collect()
        url = self.notifycfg.webhook_url
        if not url:
            messagebox.showinfo("URL未設定", "Webhook URLを入力してください")
            return

        def on_done(_r, error):
            if error is not None:
                messagebox.showerror("テスト送信失敗", str(error))
            else:
                self._set_status("Discordへテスト送信しました")
        self._submit(lambda: notify.send(url, "✅ GSM: テスト通知です(接続OK)"), on_done)

    def _crash_set_enabled(self, enabled: bool) -> None:
        self.crash_recovery = enabled
        try:
            import json as _json
            CRASH_PATH.write_text(_json.dumps({"enabled": bool(enabled)}), encoding="utf-8")
        except Exception:
            pass
        self._set_status("クラッシュ自動復旧: " + ("有効" if enabled else "無効"))

    def _publish_check(self, silent: bool = False) -> None:
        """外部公開の状態を確認(自宅レコードは自動追随)。問題時は通知。"""
        if self.config_data.publish is None:
            return
        if not silent:
            self._begin_busy("外部公開の状態を確認中…")

        def job():
            return publish.check_and_sync(self.config_data,
                                          progress=self._progress_from_worker)

        def on_done(health, error) -> None:
            if not silent:
                self._end_busy()
            if error is not None:
                self._set_status(f"外部公開チェック失敗: {error}")
                return
            # WAN IPが変わっていたら config の last_wan_ip を更新(次回の一括追随の基準)
            pub = self.config_data.publish
            if health.wan_ip and pub is not None and health.wan_ip != pub.last_wan_ip:
                try:
                    settings.update_config(
                        CONFIG_PATH, {"publish": {"last_wan_ip": health.wan_ip}})
                    pub.last_wan_ip = health.wan_ip
                except Exception:
                    pass
            self._render_publish_health(health)
            # 問題が新規に発生したときだけポップアップ通知(毎回は出さない)
            if health.needs_action and not self._publish_last_action:
                messagebox.showwarning(
                    "外部公開の対応が必要です",
                    f"{health.message}\n\n{health.instructions}")
            self._publish_last_action = health.needs_action
            if not silent:
                self._set_status(health.message)

        self._submit(job, on_done)

    def _render_publish_health(self, health) -> None:
        if not hasattr(self, "publish_status_var"):
            return
        color = {"ok": "#0a8a0a", "propagating": "#e65100",
                 "unreachable": "#c62828", "error": "#777777"}.get(health.status, "#777777")
        self.publish_status_var.set(health.message)
        self.publish_status_lbl.configure(foreground=color)
        lines = [f"現在のWAN IP : {health.wan_ip or '(取得不可)'}",
                 f"世界から見た値: {health.external_ip or '(解決不可)'}"]
        if health.synced:
            lines.append(f"自動更新した自宅レコード: {', '.join(health.synced)}")
        if health.instructions:
            lines.append("")
            lines.append(health.instructions)
        self.publish_detail.configure(state=tk.NORMAL)
        self.publish_detail.delete("1.0", tk.END)
        self.publish_detail.insert(tk.END, "\n".join(lines))
        self.publish_detail.configure(state=tk.DISABLED)

    def _save_settings(self) -> None:
        updates: dict = {}
        for path, var in self._settings_fields:
            node = updates
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = var.get().strip()
        self._begin_busy("設定を保存中…")

        def job():
            settings.update_config(CONFIG_PATH, updates)
            return load_config(CONFIG_PATH)

        def on_done(cfg, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"設定保存エラー: {error}")
                messagebox.showerror("設定保存エラー", str(error))
                return
            # 実行中のアプリに反映(サーバープロファイル/Hyper-V接続は再起動不要の範囲で)
            self.config_data = cfg
            if cfg.mysql and self.sqlshare is not None:
                self.sqlshare = SqlShareManager(cfg.mysql)
                self._sql_refresh()
            self._set_status("設定を保存しました(config.yamlに反映済み)")

        self._task_submit("アプリ設定を保存(config.yaml)",
                          job, on_done, category="設定変更", busy=False)

    # ---------- 共通ヘルパー ----------

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_status_idle(self, text: str) -> None:
        """操作実行中は上書きしない(進捗表示を守る)ステータス更新。"""
        if self._busy_count == 0:
            self.status_var.set(text)

    def _begin_busy(self, text: str) -> None:
        self._busy_count += 1
        self._set_status(text)
        if self._busy_count == 1:
            self.progress.pack(side=tk.RIGHT, padx=(4, 0))
            self.progress.start(12)

    def _end_busy(self) -> None:
        self._busy_count = max(0, self._busy_count - 1)
        if self._busy_count == 0:
            self.progress.stop()
            self.progress.pack_forget()

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert(tk.END, text)
        self.log_text.configure(state=tk.DISABLED)

    def _set_sql_text(self, text: str) -> None:
        self.sql_text.configure(state=tk.NORMAL)
        self.sql_text.delete("1.0", tk.END)
        self.sql_text.insert(tk.END, text)
        self.sql_text.configure(state=tk.DISABLED)

    def _popup_menu(self, event, tree: ttk.Treeview, menu: tk.Menu) -> None:
        row = tree.identify_row(event.y)
        # 複数選択中に、選択済みの行を右クリックした場合は選択を維持する
        if row and row not in tree.selection():
            tree.selection_set(row)
            tree.focus(row)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _selected_vm(self) -> str | None:
        sel = self.vm_tree.selection()
        if not sel:
            messagebox.showinfo("選択なし", "VMを選択してください")
            return None
        return sel[0]

    def _selected_vms(self) -> list[str]:
        """選択中の全VM名(複数選択対応)。未選択ならメッセージを出して空リスト。"""
        sel = list(self.vm_tree.selection())
        if not sel:
            messagebox.showinfo("選択なし", "VMを選択してください(Ctrl/Shiftで複数選択可)")
        return sel

    def _selected_server(self) -> GameServer | None:
        sel = self.sv_tree.selection()
        # セクション見出し(__sec__)は無視して実サーバーを選ぶ
        picked = next((s for s in sel if s in self.servers), None)
        if picked is None:
            messagebox.showinfo("選択なし", "サーバーを選択してください")
            return None
        return self.servers[picked]

    def _server_address_text(self, profile) -> str:
        """コピー/表示用のサーバーアドレス(接続に使う名前、FQDNのみ)を返す。"""
        return profile.fqdn or profile.address

    def _server_addr_port(self, profile) -> tuple[str, str]:
        """一覧表示用の (アドレス, ポート)。ポートは外部公開ポート優先。"""
        addr = profile.fqdn or profile.address
        port = profile.external_port or profile.game_port
        return addr, (str(port) if port else "-")

    def _sv_tree_dblclick(self, event) -> None:
        row = self.sv_tree.identify_row(event.y)
        if not row:
            return
        self.sv_tree.selection_set(row)
        col = self.sv_tree.identify_column(event.x)
        if col == "#0":              # サーバー名 → 名前変更
            self._sv_rename()
        elif col == "#3":            # アドレス列 → コピー
            self._sv_copy_address()

    def _sv_rename(self) -> None:
        server = self._selected_server()
        if server is None:
            return
        p = server.profile
        new_name = simpledialog.askstring(
            "名前を変更", "サーバーの表示名(エイリアス):",
            initialvalue=p.display_name, parent=self)
        if not new_name or new_name == p.display_name:
            return
        try:
            settings.update_config(
                CONFIG_PATH, {"servers": {p.name: {"display_name": new_name}}})
        except Exception as exc:
            messagebox.showerror("保存エラー", str(exc))
            return
        p.display_name = new_name
        if self.sv_tree.exists(p.name):
            self.sv_tree.item(p.name, text=new_name)
        self._set_status(f"表示名を「{new_name}」に変更しました")

    def _sv_copy_address(self) -> None:
        server = self._selected_server()
        if server is None:
            return
        addr = self._server_address_text(server.profile)
        self.clipboard_clear()
        self.clipboard_append(addr)
        self._set_status(f"アドレスをコピーしました: {addr}")

    def _selected_group(self) -> str | None:
        sel = self.sql_tree.selection()
        if not sel:
            messagebox.showinfo("選択なし", "グループを選択してください")
            return None
        return sel[0]

    # ---------- 更新 ----------

    def _auto_refresh(self) -> None:
        self.refresh_all()
        self.after(REFRESH_INTERVAL_MS, self._auto_refresh)

    def refresh_all(self) -> None:
        self._set_status_idle("更新中…")
        # 先にVM状態を取得し、その結果に応じてサーバー状態を確認する。
        # VMが停止中のサーバーはSSHを試さず即「Stop」にする(無駄な8秒タイムアウトを防ぐ→
        # ワーカーが詰まってVM起動操作が待たされる問題も解消)。
        self._submit(lambda: (self.hyperv.list_vms(), netscan.arp_table()),
                     self._on_vms_loaded)

    def _vm_ip_display(self, vm) -> str:
        """VMのIPを解決して表示形式にする(同一セグメントは第4オクテットのみ)。"""
        profile_ips = {s.profile.vm: s.profile.address
                       for s in self.servers.values() if s.profile.vm}
        ip = profile_ips.get(vm.name) or vm.ip_hint \
            or (self._arp_cache.get(vm.mac) if vm.mac else None)
        if not ip:
            return "-"
        prefix = self.config_data.network.prefix + "."
        return "." + ip[len(prefix):] if ip.startswith(prefix) else ip

    def _on_vms_loaded(self, result, error) -> None:
        if error is not None:
            self._set_status_idle(f"Hyper-V接続エラー: {error}")
            return
        vms, self._arp_cache = result
        self._vm_states = {vm.name: vm.state for vm in vms}
        selected = self.vm_tree.selection()
        self.vm_tree.delete(*self.vm_tree.get_children())
        for vm in vms:
            text, tag = VM_STATE_VIEW.get(vm.state, (vm.state, "busy"))
            self.vm_tree.insert("", tk.END, iid=vm.name, text=vm.name, tags=(tag,),
                                values=(text, self._vm_ip_display(vm),
                                        vm.cpu_usage, vm.memory_mb, vm.uptime_text))
        for iid in selected:
            if self.vm_tree.exists(iid):
                self.vm_tree.selection_add(iid)
        self._set_status_idle("更新完了")
        # VM状態に応じてサーバー状態を更新(依存関係)
        self._refresh_server_statuses()

    def _refresh_server_statuses(self) -> None:
        """VMが稼働中のサーバーだけSSHで状態確認。停止中のVMのサーバーは即Stop。"""
        for name, server in self.servers.items():
            vmname = server.profile.vm
            vm_state = self._vm_states.get(vmname) if vmname else None
            # VMが紐づいていて、かつ稼働中でない → SSHせずStop(接続不可はエラーではない)
            if vmname and vm_state is not None and vm_state != "Running":
                self._set_server_status(name, "Stop", "off")
                continue
            if vmname and vm_state is None:
                # VMが一覧に見つからない(名前ずれ等)は不明扱い
                self._set_server_status(name, "VM不明", "busy")
                continue
            # VM稼働中 or VM未紐付け → 実際にSSHで確認
            self._submit(server.status,
                         lambda result, error, n=name: self._on_server_status(n, result, error))

    def _set_server_status(self, name: str, text: str, tag: str) -> None:
        if self.sv_tree.exists(name):
            self.sv_tree.set(name, "status", text)
            self.sv_tree.item(name, tags=(tag,))

    def _on_server_status(self, name: str, status, error) -> None:
        if not self.sv_tree.exists(name):
            return
        raw = str(error) if error is not None else status
        text, tag = server_status_view(raw)
        self.sv_tree.set(name, "status", text)
        self.sv_tree.item(name, tags=(tag,))
        new_run = (raw == "active")
        prev = self._server_running.get(name)            # None=初回(未取得)
        self._server_running[name] = new_run
        if prev is None:
            self._portsync_on_change()                   # 初回は開閉のみ(通知/復旧なし)
        elif prev != new_run:                            # 変化: ポート+通知+クラッシュ判定
            self._server_state_changed(f"mc:{name}", "mc",
                                       self.servers[name].profile.display_name,
                                       new_run, self.servers[name])
        # 稼働中でバージョン未取得なら裏で取得する(セッション中1回だけ)
        if raw == "active" and name not in self._versions \
                and name not in self._version_fetching:
            self._fetch_version(name, announce=False)

    # ---------- バージョン / サーバー情報 ----------

    def _resolve_fqdns(self) -> None:
        """各サーバーのFQDNを裏で解決してキャッシュする(表示用)。"""
        for name, server in self.servers.items():
            fqdn = server.profile.fqdn
            if not fqdn:
                continue

            def on_done(ip, error, n=name) -> None:
                if error is None and ip:
                    self._fqdn_ips[n] = ip

            self._submit(lambda h=fqdn: socket.gethostbyname(h), on_done)

    def _fetch_version(self, name: str, announce: bool) -> None:
        server = self.servers[name]
        self._version_fetching.add(name)

        def on_done(version, error) -> None:
            self._version_fetching.discard(name)
            version = version if (error is None and version) else "?"
            self._versions[name] = version
            if self.sv_tree.exists(name):
                self.sv_tree.set(name, "version", version)
            if announce:
                self._append_log(self._info_text(name))
                self._set_status(f"{server.profile.display_name} 起動完了")

        self._submit(server.detect_version, on_done)

    def _info_text(self, name: str) -> str:
        p = self.servers[name].profile
        version = self._versions.get(name, "?")
        port = f":{p.game_port}" if p.game_port else ""
        lines = [f"■ {p.display_name} ({p.name})"]
        if p.fqdn:
            resolved = self._fqdn_ips.get(name)
            arrow = f" (→ {resolved})" if resolved else ""
            lines.append(f"  FQDN        : {p.fqdn}{port}{arrow}")
        lines += [
            f"  LANアドレス : {p.address}{port}",
            f"  バージョン  : {version}",
            f"  VM          : {p.vm or '-'}",
            f"  systemd     : {p.service or '-'}",
        ]
        if p.rcon:
            lines.append(f"  RCON        : {p.address}:{p.rcon.port}")
        return "\n".join(lines)

    def _sv_info(self) -> None:
        server = self._selected_server()
        if server is None:
            return
        name = server.profile.name
        if name not in self._versions:
            self._begin_busy("サーバー情報を取得中…")

            def on_done(version, error) -> None:
                self._end_busy()
                self._versions[name] = version if (error is None and version) else "?"
                if self.sv_tree.exists(name):
                    self.sv_tree.set(name, "version", self._versions[name])
                self._append_log(self._info_text(name))
                self._set_status("完了")

            self._submit(server.detect_version, on_done)
        else:
            self._append_log(self._info_text(name))

    # ---------- VM操作 ----------

    def _vm_mark_busy(self, name: str, text: str) -> None:
        if self.vm_tree.exists(name):
            self.vm_tree.set(name, "state", text)
            self.vm_tree.item(name, tags=("busy",))

    def _vm_bulk(self, names: list[str], job_factory, busy_verb: str,
                 done_verb: str, mark: str) -> None:
        """複数VMに対して起動/停止などを一括実行(ワーカーで順次)。"""
        if not names:
            return
        for name in names:
            self._vm_mark_busy(name, mark)
        self._begin_busy(f"{len(names)}台のVMを{busy_verb}中… ({', '.join(names)})")
        remaining = {"n": len(names), "errors": []}

        def make_done(vm: str):
            def on_done(_result, error) -> None:
                if error is not None:
                    remaining["errors"].append(f"{vm}: {error}")
                remaining["n"] -= 1
                if remaining["n"] == 0:
                    self._end_busy()
                    if remaining["errors"]:
                        self._set_status(f"{done_verb}: 一部失敗")
                        messagebox.showerror(
                            f"VM{done_verb}エラー", "\n".join(remaining["errors"]))
                    else:
                        self._set_status(f"{len(names)}台のVMを{done_verb}しました")
                    self.refresh_all()
            return on_done

        for name in names:
            self._task_submit(f"VM {name} を{busy_verb}",
                              lambda v=name: job_factory(v), make_done(name),
                              category=f"VM{busy_verb}", busy=False)

    def _vm_start(self) -> None:
        names = self._selected_vms()
        if not names:
            return
        self._vm_bulk(names, lambda v: self.hyperv.start_vm(v),
                      "起動", "起動", "起動中…")

    def _vm_stop_job(self, vm: str, force: bool) -> None:
        """VMを止める前に、そのVM上のゲームサービスを安全に停止(保存)する。

        systemctl stop は SIGTERM を送り、Minecraft等はシャットダウンフックで
        ワールドを保存して終了する(停止完了までブロック)。これを待ってから
        VMを落とすので、強制停止でもデータ損失を防げる。サービス停止はベスト
        エフォート(失敗してもVM停止は続行)。
        """
        for server in self.servers.values():
            p = server.profile
            if p.vm != vm or not p.service:
                continue
            try:
                if server.status() == "active":
                    self._progress_from_worker(
                        f"{p.display_name}: 保存して停止中…(VM停止前)")
                    server.stop()   # systemctl stop = 保存待ち
            except Exception:
                pass  # 接続不可等でもVM停止は続行
        self.hyperv.stop_vm(vm, force=force)

    def _vm_stop(self) -> None:
        names = self._selected_vms()
        if not names:
            return
        label = "これらのVM" if len(names) > 1 else f"VM「{names[0]}」"
        if not messagebox.askyesno(
                "確認",
                f"{label}をシャットダウンしますか?\n\n" + "\n".join(names)
                + "\n\n※ 稼働中のゲームサーバーは先に安全停止(保存)します"):
            return
        self._vm_bulk(names, lambda v: self._vm_stop_job(v, False),
                      "シャットダウン", "シャットダウン", "停止中…")

    def _vm_force_stop(self) -> None:
        names = self._selected_vms()
        if not names:
            return
        label = "これらのVM" if len(names) > 1 else f"VM「{names[0]}」"
        if not messagebox.askyesno(
                "⚡ 強制停止",
                f"{label}を強制停止しますか?\n\n" + "\n".join(names) + "\n\n"
                "電源を即断します(電源ケーブルを抜くのと同じ)。\n"
                "※ 稼働中のゲームサーバーは先に安全停止(保存)してから強制停止します。\n"
                "通常は「シャットダウン」を使ってください。",
                icon="warning", default="no"):
            return
        self._vm_bulk(names, lambda v: self._vm_stop_job(v, True),
                      "強制停止", "強制停止", "強制停止中…")

    def _vm_settings(self) -> None:
        name = self._selected_vm()
        if name is None:
            return
        current_mem = self.vm_tree.set(name, "mem")

        dialog = tk.Toplevel(self)
        dialog.title(f"VM設定: {name}")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 250, self.winfo_rooty() + 200))
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill=tk.BOTH, expand=True)
        ttk.Label(form, text="※ VMが停止中のときだけ変更できます",
                  foreground="#777777").grid(row=0, column=0, columnspan=2, sticky=tk.W)
        ttk.Label(form, text="メモリ(MB)").grid(row=1, column=0, sticky=tk.W, pady=4)
        mem_var = tk.StringVar(value=current_mem if current_mem.isdigit() else "2048")
        ttk.Entry(form, textvariable=mem_var, width=12).grid(row=1, column=1, sticky=tk.W)
        ttk.Label(form, text="CPU数").grid(row=2, column=0, sticky=tk.W, pady=4)
        cpu_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=cpu_var, width=12).grid(row=2, column=1, sticky=tk.W)
        ttk.Label(form, text="(空欄=変更しない)",
                  foreground="#777777").grid(row=3, column=0, columnspan=2, sticky=tk.W)

        def apply() -> None:
            try:
                mem = int(mem_var.get()) if mem_var.get().strip() else None
                cpu = int(cpu_var.get()) if cpu_var.get().strip() else None
            except ValueError:
                messagebox.showerror("入力エラー", "数値を入力してください", parent=dialog)
                return
            if mem is None and cpu is None:
                dialog.destroy()
                return
            dialog.destroy()
            self._begin_busy(f"VM {name} の設定を変更中…")
            self._task_submit(f"VM {name} の設定変更(CPU/メモリ)",
                              lambda: self.hyperv.set_vm_resources(name, mem, cpu),
                              self._make_action_done(f"VM {name} の設定変更完了"),
                              category="VM設定変更", busy=False)

        btns = ttk.Frame(form)
        btns.grid(row=4, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="適用", command=apply).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT)

    def _vm_duplicate(self) -> None:
        """選択VMを複製する(ゲーム構築なし)。複製後、ホスト名/IPを個体化する。"""
        name = self._selected_vm()
        if name is None:
            return
        current_mem = self.vm_tree.set(name, "mem")
        net = self.config_data.network
        first = next(iter(self.servers.values()), None)
        du = first.profile.ssh_user if first else "master"
        dp = (first.profile.ssh_password or "") if first else ""

        dialog = tk.Toplevel(self)
        dialog.title(f"VMを複製: {name}")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 180, self.winfo_rooty() + 120))
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill=tk.BOTH, expand=True)
        ttk.Label(form, text=f"複製元: {name}  ※ 複製元VMは停止しておくこと",
                  foreground="#777777").grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 6))

        os_var = tk.StringVar(value="windows")
        osf = ttk.Frame(form)
        osf.grid(row=1, column=0, columnspan=2, sticky=tk.W)
        ttk.Label(form, text="OS種別(個体化方法)").grid(row=1, column=0, sticky=tk.W)
        ttk.Radiobutton(osf, text="   Windows", variable=os_var, value="windows").pack(side=tk.LEFT, padx=(140, 0))
        ttk.Radiobutton(osf, text="Linux", variable=os_var, value="linux").pack(side=tk.LEFT, padx=6)

        rows = [
            ("新VM名", "new_vm", f"{name}-copy"),
            ("新ホスト名", "hostname", ""),
            (f"新IP({net.prefix}.X のX)", "new_ip", ""),
            ("メモリ(MB)", "memory", current_mem if current_mem.isdigit() else "4096"),
            ("CPU数", "cpu", "2"),
            (f"複製元IP({net.prefix}.X ※Linux個体化用)", "src_ip", ""),
            ("ゲストユーザー", "guser", du),
            ("ゲストパスワード", "gpass", dp),
        ]
        v: dict[str, tk.StringVar] = {}
        for i, (label, key, default) in enumerate(rows, start=2):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky=tk.W, pady=2)
            var = tk.StringVar(value=default)
            v[key] = var
            ttk.Entry(form, textvariable=var, width=28,
                      show="*" if key == "gpass" else "").grid(
                row=i, column=1, sticky=tk.W, padx=(8, 0), pady=2)
        ttk.Label(form, text="※ config.yamlには登録しません(ゲームサーバーではないため)",
                  foreground="#777777").grid(row=len(rows) + 2, column=0, columnspan=2,
                                             sticky=tk.W, pady=(6, 0))

        def start() -> None:
            new_vm = v["new_vm"].get().strip()
            hostname = v["hostname"].get().strip()
            os_kind = os_var.get()
            if not new_vm or not hostname or not v["new_ip"].get().strip():
                messagebox.showerror("入力不足", "新VM名・ホスト名・新IPは必須です", parent=dialog)
                return
            try:
                new_ip = net.full_ip(v["new_ip"].get())
                mem = int(v["memory"].get()); cpu = int(v["cpu"].get())
            except ValueError as exc:
                messagebox.showerror("入力エラー", str(exc), parent=dialog)
                return
            src_ip = net.full_ip(v["src_ip"].get()) if v["src_ip"].get().strip() else ""
            if os_kind == "linux" and not src_ip:
                messagebox.showerror("入力不足",
                                     "Linuxの個体化には複製元IPが必要です", parent=dialog)
                return
            guser, gpass = v["guser"].get(), v["gpass"].get()
            dialog.destroy()
            gateway = net.gateway
            dns = self.config_data.dns.host if self.config_data.dns else f"{net.prefix}.254"
            self._begin_busy(f"{name} を {new_vm} に複製中…(数分)")

            def job() -> None:
                if netscan.ip_in_use(new_ip):
                    raise RuntimeError(f"{new_ip} は既に使用中です")
                self._progress_from_worker("ディスク複製中…(差分は平坦化。数分)")
                self.hyperv.duplicate_vm(name, new_vm, mem, cpu, start=True)
                if os_kind == "windows":
                    self.hyperv.individualize_windows(
                        new_vm, guser, gpass, hostname, new_ip, gateway, dns,
                        progress=self._progress_from_worker)
                else:
                    from core.orchestration import _wait_for_port, individualize_clone
                    self._progress_from_worker(f"クローン起動待ち({src_ip})…")
                    _wait_for_port(src_ip, 22, 300)
                    individualize_clone(src_ip, guser, gpass, hostname, new_ip,
                                        gateway, dns, progress=self._progress_from_worker)

            def on_done(_r, error) -> None:
                self._end_busy()
                if error is not None:
                    self._set_status(f"複製失敗: {error}")
                    messagebox.showerror("複製失敗", str(error))
                else:
                    self._set_status(f"{new_vm} を作成しました({new_ip})")
                    self._append_log(f"■ VM複製完了: {name} → {new_vm}"
                                     f"(ホスト名 {hostname} / {new_ip})")
                self.refresh_all()

            self._task_submit(f"VM複製 {name} → {new_vm}({hostname}/{new_ip})",
                              job, on_done, category="VM複製", busy=False)

        btns = ttk.Frame(form)
        btns.grid(row=len(rows) + 3, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="複製開始", command=start).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT)

    def _vm_change_ip(self) -> None:
        name = self._selected_vm()
        if name is None:
            return
        net = self.config_data.network
        profile = next((s.profile for s in self.servers.values()
                        if s.profile.vm == name), None)
        current_ip = self.vm_tree.set(name, "ip")
        if current_ip.startswith("."):
            current_ip = net.prefix + current_ip
        first = next(iter(self.servers.values()), None)
        ssh_user = profile.ssh_user if profile else (
            first.profile.ssh_user if first else "master")
        ssh_pass = (profile.ssh_password if profile else
                    (first.profile.ssh_password if first else "")) or ""

        dialog = tk.Toplevel(self)
        dialog.title(f"IP変更: {name}")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 250, self.winfo_rooty() + 180))
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill=tk.BOTH, expand=True)
        ttk.Label(form, text="※ VMを再起動してIPを変更します。\n"
                             "   DNS(A/PTR)とconfig.yamlも自動で追随します。",
                  foreground="#777777").grid(row=0, column=0, columnspan=2, sticky=tk.W)
        entries = [
            ("現在のIP", "cur", current_ip if "." in current_ip else ""),
            (f"新IP({net.prefix}.X のXのみ可)", "new", ""),
            ("SSHユーザー", "user", ssh_user),
            ("SSHパスワード", "pass", ssh_pass),
        ]
        vars_: dict[str, tk.StringVar] = {}
        for i, (label, key, default) in enumerate(entries, start=1):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky=tk.W, pady=3)
            var = tk.StringVar(value=default)
            vars_[key] = var
            ttk.Entry(form, textvariable=var, width=26,
                      show="*" if key == "pass" else "").grid(
                row=i, column=1, sticky=tk.W, padx=(8, 0), pady=3)

        def submit() -> None:
            try:
                cur = net.full_ip(vars_["cur"].get())
                new = net.full_ip(vars_["new"].get())
            except (ValueError, IndexError) as exc:
                messagebox.showerror("入力エラー", str(exc), parent=dialog)
                return
            if not vars_["pass"].get():
                messagebox.showerror("入力不足", "SSHパスワードを入力してください", parent=dialog)
                return
            if cur == new:
                messagebox.showinfo("変更なし", "同じIPです", parent=dialog)
                return
            user, password = vars_["user"].get().strip(), vars_["pass"].get()
            dialog.destroy()
            self._begin_busy(f"{name} のIPを {cur} → {new} に変更中…(再起動を伴います)")

            def job() -> list[str]:
                warnings: list[str] = []
                if netscan.ip_in_use(new):
                    raise RuntimeError(f"{new} は既に使用中です(ping/SSH応答あり)")
                dns_ip = self.config_data.dns.host if self.config_data.dns \
                    else "192.168.11.254"
                change_vm_ip(cur, user, password, new, net.gateway, dns_ip,
                             progress=self._progress_from_worker)
                if self.config_data.dns:
                    dnsreg.update_ip(self.config_data.dns, cur, new,
                                     progress=self._progress_from_worker)
                if profile is not None:
                    settings.update_config(
                        CONFIG_PATH, {"servers": {profile.name: {"address": new}}})
                if self.sqlshare is not None:
                    # SQL共有グループのアクセス権(IP限定ユーザー)も付け替える
                    self._progress_from_worker("SQL共有グループのアクセス権を更新中…")
                    try:
                        changed = self.sqlshare.update_member_ip(cur, new)
                        if changed:
                            self._progress_from_worker(
                                f"SQL共有更新: {', '.join(changed)}")
                    except Exception as exc:
                        warnings.append(f"SQL共有の付け替えに失敗: {exc}\n"
                                        f"SQL共有タブで手動で除外→再追加してください")
                return warnings

            def on_done(warnings, error) -> None:
                self._end_busy()
                if error is not None:
                    self._set_status(f"IP変更エラー: {error}")
                    messagebox.showerror("IP変更エラー", str(error))
                    self.refresh_all()
                    return
                for warn in warnings or []:
                    messagebox.showwarning("IP変更は成功(一部注意)", warn)
                if profile is not None:
                    # 実行中のアプリにも新アドレスを反映
                    cfg = load_config(CONFIG_PATH)
                    self.config_data = cfg
                    new_profile = next(p for p in cfg.servers if p.name == profile.name)
                    self.servers[profile.name].close()
                    self.servers[profile.name] = GameServer(new_profile)
                    addr, port = self._server_addr_port(new_profile)
                    if self.sv_tree.exists(profile.name):
                        self.sv_tree.set(profile.name, "address", addr)
                        self.sv_tree.set(profile.name, "port", port)
                self._set_status(f"{name} のIP変更完了: {new}")
                self.refresh_all()

            self._task_submit(f"VM {name} のIP変更 {cur} → {new}",
                              job, on_done, category="IP変更", busy=False)

        btns = ttk.Frame(form)
        btns.grid(row=len(entries) + 1, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="変更実行", command=submit).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT)

    # ---------- サーバー操作 ----------

    def _sv_action(self, action: str) -> None:
        server = self._selected_server()
        if server is None:
            return
        name = server.profile.name
        label = {"start": "起動", "stop": "停止", "restart": "再起動"}[action]
        if action != "start" and not messagebox.askyesno(
                "確認", f"「{server.profile.display_name}」を{label}しますか?"):
            return
        if action == "stop":
            self._mark_stop(f"mc:{name}")
        elif action == "restart":
            self._mark_restart(f"mc:{name}")
        if self.sv_tree.exists(name):
            self.sv_tree.set(name, "status", f"{label}中…")
            self.sv_tree.item(name, tags=("busy",))
        self._begin_busy(f"{server.profile.display_name} を{label}中…")

        after = None
        if action in ("start", "restart"):
            # 起動後にバージョン等のサーバー情報をログ欄に表示する
            after = lambda: self._fetch_version(name, announce=True)

        if action == "start":
            # VMが停止していれば自動でVM起動→SSH応答待ち→サービス起動
            job = lambda: start_server_with_vm(
                self.hyperv, server, progress=self._progress_from_worker)
        else:
            job = lambda: getattr(server, action)()
        self._task_submit(
            f"{server.profile.display_name} を{label}",
            job,
            self._make_action_done(f"{server.profile.display_name} {label}完了",
                                   after=after),
            category=f"サーバー{label}", busy=False)

    def _make_action_done(self, ok_text: str, after=None):
        def on_done(_result, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"エラー: {error}")
                messagebox.showerror("エラー", str(error))
            else:
                self._set_status(ok_text)
                if after is not None:
                    after()
            self.refresh_all()
        return on_done

    def _sv_players(self) -> None:
        server = self._selected_server()
        if server is None:
            return
        name = server.profile.name
        self._begin_busy("プレイヤー情報を取得中…")

        def on_done(result, error) -> None:
            self._end_busy()
            if error is not None:
                text = str(error)
                cell = text
            else:
                text = result
                parsed = server.parse_players(result)
                if parsed is not None:
                    count, max_players, names = parsed
                    cell = f"{count}/{max_players}" if max_players is not None else str(count)
                    if names:
                        cell += f" ({names})"
                    text = cell
                else:
                    cell = result.replace("\n", " / ")
            if self.sv_tree.exists(name):
                self.sv_tree.set(name, "players", cell)
            self._append_log(text)
            self._set_status("完了")

        self._submit(server.players, on_done)

    def _sv_op(self) -> None:
        """参加中プレイヤーへのOP権限(いわゆるroot)の付与/剥奪。"""
        server = self._selected_server()
        if server is None:
            return
        if server.profile.rcon is None:
            messagebox.showerror("RCON未設定", "このサーバーはRCONが設定されていません")
            return

        dialog = tk.Toplevel(self)
        dialog.title(f"OP管理: {server.profile.display_name}")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 250, self.winfo_rooty() + 200))
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill=tk.BOTH, expand=True)
        ttk.Label(form, text="プレイヤー名(参加中から選択 or 手入力):").pack(anchor=tk.W)
        player_var = tk.StringVar()
        combo = ttk.Combobox(form, textvariable=player_var, width=30)
        combo.pack(anchor=tk.W, pady=6)
        status = tk.StringVar(value="参加中プレイヤーを取得中…")
        ttk.Label(form, textvariable=status, foreground="#777777").pack(anchor=tk.W)

        def on_players(result, error) -> None:
            if not dialog.winfo_exists():
                return
            if error is not None:
                status.set(f"取得失敗: {error}")
                return
            parsed = server.parse_players(result)
            names = [n.strip() for n in (parsed[2].split(",") if parsed and parsed[2]
                                         else []) if n.strip()]
            combo["values"] = names
            if names:
                combo.current(0)
                status.set(f"参加中: {len(names)}人")
            else:
                status.set("参加中のプレイヤーはいません(手入力は可能)")

        self._submit(server.players, on_players)

        def run_cmd(cmd_name: str, label: str) -> None:
            player = player_var.get().strip()
            if not player:
                messagebox.showerror("入力不足", "プレイヤー名を入力してください", parent=dialog)
                return
            dialog.destroy()
            self._begin_busy(f"{player} に{label}を実行中…")

            def on_done(result, error) -> None:
                self._end_busy()
                body = str(error) if error is not None else (result.strip() or "(応答なし)")
                self._append_log(f"> {cmd_name} {player}\n{body}")
                self._set_status(f"{label}完了: {player}")

            self._task_submit(f"{server.profile.display_name}: {label} {player}",
                              lambda: server.rcon_command(f"{cmd_name} {player}"),
                              on_done, category="プレイヤー操作", busy=False)

        btns = ttk.Frame(form)
        btns.pack(pady=(12, 0))
        ttk.Button(btns, text="👑 OP付与",
                   command=lambda: run_cmd("op", "OP付与")).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="OP剥奪",
                   command=lambda: run_cmd("deop", "OP剥奪")).pack(side=tk.LEFT)
        ttk.Button(btns, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT, padx=6)

    def _cfg_label_cell(self, parent, row: int, key: str) -> None:
        """設定行のラベル。col0=日本語(無ければキー名)、翻訳がある時はcol2に元キー名を淡色で。"""
        jp = PROP_LABELS_JA.get(key)
        ttk.Label(parent, text=(jp or key)).grid(
            row=row, column=0, sticky=tk.W, padx=6, pady=3)
        if jp:
            ttk.Label(parent, text=key, foreground=PAL["muted"]).grid(
                row=row, column=2, sticky=tk.W, padx=(4, 6))

    def _pal_server_config(self, server) -> None:
        """Palworldの詳細設定(PalWorldSettings.ini)を日本語UIで編集する。"""
        p = server.profile
        self._begin_busy(f"{p.display_name}: 設定を読込中…")

        def on_done(opt, error):
            self._end_busy()
            if error is not None:
                self._set_status(f"設定読込エラー: {error}")
                messagebox.showerror("読込エラー", str(error))
                return
            self._open_pal_config_dialog(server, opt)

        self._submit(lambda: palconfig.read(p), on_done)

    def _open_pal_config_dialog(self, server, opt) -> None:
        p = server.profile
        dialog = tk.Toplevel(self)
        dialog.title(f"Palworld 詳細設定: {p.display_name}")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("700x640+%d+%d"
                        % (self.winfo_rootx() + 110, self.winfo_rooty() + 40))
        ttk.Label(dialog, foreground=PAL["muted"], justify=tk.LEFT, padding=(10, 8, 10, 0),
                  text="各欄は現在値(未設定はゲーム既定値)。変更した項目だけ保存されます。\n"
                       "反映には「保存して再起動」またはサーバー再起動が必要です。").pack(anchor=tk.W)
        nb = ttk.Notebook(dialog)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        fields = []                              # (key, kind, getter, initial)
        for tab_label, specs in PAL_SETTINGS_TABS:
            tab = self._scrollable_tab(nb, f" {tab_label} ")
            for row, (key, kind, label, default, choices) in enumerate(specs):
                cur = opt.get(key)
                shown = cur if cur is not None else default
                ttk.Label(tab, text=label, anchor=tk.W, wraplength=360).grid(
                    row=row, column=0, sticky=tk.W, padx=(8, 6), pady=3)
                if kind == "bool":
                    checked = (shown or "").strip().lower() == "true"
                    var = tk.BooleanVar(value=checked)
                    ttk.Checkbutton(tab, variable=var).grid(row=row, column=1, sticky=tk.W, padx=6)
                    getter = lambda v=var: "True" if v.get() else "False"
                    initial = "True" if checked else "False"
                elif kind == "choice":
                    jp_by_val = {v: j for v, j in choices}
                    val_by_jp = {j: v for v, j in choices}
                    cur_v = shown or default
                    disp = [j for _, j in choices]
                    cur_jp = jp_by_val.get(cur_v, cur_v)
                    if cur_jp not in disp:
                        disp = [cur_jp] + disp
                    var = tk.StringVar(value=cur_jp)
                    ttk.Combobox(tab, textvariable=var, values=disp, state="readonly",
                                 width=22).grid(row=row, column=1, sticky=tk.W, padx=6)
                    getter = lambda v=var, m=val_by_jp: m.get(v.get(), v.get())
                    initial = cur_v
                else:                            # float / int / str
                    var = tk.StringVar(value=shown or "")
                    ttk.Entry(tab, textvariable=var, width=22 if kind == "str" else 12).grid(
                        row=row, column=1, sticky=tk.W, padx=6)
                    getter = lambda v=var: v.get().strip()
                    initial = shown or ""
                fields.append((key, kind, getter, initial))
            tab.columnconfigure(0, weight=1)

        def do_save(restart):
            applied = []
            for key, kind, getter, initial in fields:
                val = getter()
                if val == initial:
                    continue
                if kind in ("float", "int"):
                    if val == "":
                        continue
                    try:
                        float(val) if kind == "float" else int(val)
                    except ValueError:
                        messagebox.showerror("入力エラー",
                                             f"「{key}」は数値で入力してください: {val}",
                                             parent=dialog)
                        return
                    opt.set(key, val)
                elif kind == "str":
                    opt.set(key, f'"{val}"')     # 文字列は"で囲む
                else:                            # bool / choice
                    opt.set(key, val)
                applied.append(f"{key}={val}")
            if not applied:
                messagebox.showinfo("変更なし", "変更された項目はありません。", parent=dialog)
                return
            dialog.destroy()

            def job():
                palconfig.write(p, opt, restart=restart,
                                progress=self._progress_from_worker)
                return applied

            def on_done(_r, error):
                if error is not None:
                    self._set_status(f"Palworld設定 保存エラー: {error}")
                    messagebox.showerror("保存エラー", str(error))
                    return
                extra = "(再起動済み)" if restart else "(反映には再起動が必要)"
                self._set_status(f"Palworld設定を保存しました {extra}")
                self._append_log("■ Palworld詳細設定を更新: " + ", ".join(applied) + f"\n  {extra}")

            title = "Palworld詳細設定を保存" + ("+再起動" if restart else "")
            self._task_submit(title, job, on_done, category="設定変更", busy=True)

        btns = ttk.Frame(dialog, padding=(8, 6))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Button(btns, text="キャンセル", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="💾 保存して再起動",
                   command=lambda: do_save(True)).pack(side=tk.RIGHT, padx=6)
        ttk.Button(btns, text="保存のみ",
                   command=lambda: do_save(False)).pack(side=tk.RIGHT)

    def _sv_mod_manager(self) -> None:
        server = self._selected_server()
        if server is None:
            messagebox.showinfo("選択なし", "サーバーを選んでください")
            return
        p = server.profile
        if p.game != "minecraft":
            messagebox.showinfo("未対応", "Mod管理はMinecraft(Fabric)サーバー用です。")
            return
        self._open_mod_manager_dialog(p)

    def _open_mod_manager_dialog(self, p) -> None:
        api_key = self.config_data.curseforge_api_key
        dlg = tk.Toplevel(self)
        dlg.title(f"🧩 Mod管理 — {p.display_name}")
        dlg.geometry("860x640+%d+%d"
                     % (self.winfo_rootx() + 120, self.winfo_rooty() + 40))
        dlg.transient(self)
        frm = ttk.Frame(dlg, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)
        state = {"installed": [], "results": []}

        top = ttk.Frame(frm)
        top.pack(fill=tk.X)
        ttk.Label(top, text="MCバージョン:").pack(side=tk.LEFT)
        mcver_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=mcver_var, width=12).pack(side=tk.LEFT, padx=(4, 8))
        status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=status_var, foreground=PAL["muted"]).pack(side=tk.LEFT)
        if not api_key:
            ttk.Label(top, text="(CurseForgeキー未設定=Modrinthのみ)",
                      foreground="#ffd166").pack(side=tk.RIGHT)

        def busy(msg=""):
            status_var.set(msg)
            dlg.update_idletasks()

        def detect_mcver(mods):
            fa = next((m for m in mods if m.get("id") == "fabric-api"), None)
            if fa and "+" in fa.get("version", ""):
                return fa["version"].split("+")[-1]
            return ""

        # --- 導入済みMod ---
        inst_lf = ttk.LabelFrame(frm, text="導入済みMod")
        inst_lf.pack(fill=tk.BOTH, expand=True, pady=(8, 4))
        inst_tree = ttk.Treeview(inst_lf, columns=("version", "update"),
                                 show="tree headings", height=8)
        inst_tree.heading("#0", text="Mod")
        inst_tree.heading("version", text="現在の版")
        inst_tree.heading("update", text="更新")
        inst_tree.column("#0", width=300)
        inst_tree.column("version", width=220, anchor=tk.CENTER)
        inst_tree.column("update", width=160, anchor=tk.CENTER)
        inst_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        def refresh_installed():
            busy("導入済みModを取得中…")
            self._submit(
                lambda: modmanager.list_installed_meta(p),
                lambda mods, err: _installed_done(mods, err))

        def _installed_done(mods, err):
            if err is not None or not dlg.winfo_exists():
                busy(f"取得失敗: {err}" if err else "")
                return
            state["installed"] = mods
            inst_tree.delete(*inst_tree.get_children())
            for i, m in enumerate(mods):
                inst_tree.insert("", tk.END, iid=str(i), text=m["name"],
                                 values=(m["version"], ""))
            if not mcver_var.get():
                mcver_var.set(detect_mcver(mods))
            busy(f"導入済み {len(mods)} 件")

        def check_updates():
            mc = mcver_var.get().strip()
            if not mc:
                messagebox.showwarning("MCバージョン", "MCバージョンを入力してください",
                                       parent=dlg)
                return
            busy("更新を確認中…(Modrinth照合)")
            self._submit(
                lambda: modmanager.check_updates_modrinth(p, mc),
                lambda ups, err: _updates_done(ups, err))

        def _updates_done(ups, err):
            if err is not None or not dlg.winfo_exists():
                busy(f"更新確認失敗: {err}" if err else "")
                return
            by_file = {u["file"]: u for u in ups}
            for i, m in enumerate(state["installed"]):
                u = by_file.get(m["file"])
                if not u:
                    continue
                if u["source"] != "modrinth":
                    txt = "─(CFは再導入で更新)"
                elif u["update"]:
                    txt = f"🔺 {u['latest']}"
                else:
                    txt = "✓ 最新"
                inst_tree.set(str(i), "update", txt)
            busy("更新確認 完了")

        def remove_selected():
            sel = inst_tree.selection()
            if not sel:
                return
            m = state["installed"][int(sel[0])]
            if not messagebox.askyesno(
                    "Mod削除",
                    f"{m['name']} を削除してサーバーを再起動します。よろしいですか?\n"
                    f"({m['file']})", parent=dlg):
                return
            busy("削除して再起動中…")
            self._task_submit(
                f"🗑 Mod削除: {m['name']} ({p.display_name})",
                lambda: modmanager.remove_mods(p, [m["file"]], restart=True,
                                               progress=self._progress_from_worker),
                lambda _r, err: (busy("削除完了" if not err else f"失敗: {err}"),
                                 refresh_installed()),
                category="Mod管理", busy=False)

        ib = ttk.Frame(inst_lf)
        ib.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Button(ib, text="🔄 一覧更新", command=refresh_installed).pack(side=tk.LEFT)
        ttk.Button(ib, text="⬆ 更新確認", command=check_updates).pack(side=tk.LEFT, padx=6)
        ttk.Button(ib, text="🗑 選択を削除", command=remove_selected).pack(side=tk.LEFT)

        # --- 検索・追加 ---
        src_lf = ttk.LabelFrame(frm, text="Mod検索・追加 (Modrinth + CurseForge / 依存は自動解決)")
        src_lf.pack(fill=tk.BOTH, expand=True, pady=(6, 4))
        sb = ttk.Frame(src_lf)
        sb.pack(fill=tk.X, padx=6, pady=6)
        q_var = tk.StringVar()
        q_entry = ttk.Entry(sb, textvariable=q_var)
        q_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        res_tree = ttk.Treeview(src_lf, columns=("source", "downloads"),
                                show="tree headings", height=7)
        res_tree.heading("#0", text="Mod")
        res_tree.heading("source", text="ソース")
        res_tree.heading("downloads", text="DL数")
        res_tree.column("#0", width=360)
        res_tree.column("source", width=110, anchor=tk.CENTER)
        res_tree.column("downloads", width=110, anchor=tk.E)
        res_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        def do_search():
            q = q_var.get().strip()
            mc = mcver_var.get().strip()
            if not q or not mc:
                messagebox.showwarning(
                    "入力", "MCバージョンと検索語を入力してください", parent=dlg)
                return
            busy(f"検索中…({q})")
            self._submit(
                lambda: onlinemods.search(q, mc, api_key),
                lambda res, err: _search_done(res, err))

        def _search_done(res, err):
            if not dlg.winfo_exists():
                return
            if err is not None:
                busy(f"検索失敗: {err}")
                return
            state["results"] = res
            res_tree.delete(*res_tree.get_children())
            for i, r in enumerate(res):
                res_tree.insert("", tk.END, iid=str(i), text=r["name"],
                                values=(r["source"], f"{r['downloads']:,}"))
            busy(f"{len(res)} 件ヒット")

        def install_selected():
            sel = res_tree.selection()
            if not sel:
                return
            r = state["results"][int(sel[0])]
            mc = mcver_var.get().strip()
            busy(f"依存を解決中…({r['name']})")
            self._submit(
                lambda: onlinemods.collect_with_deps(r["source"], r["id"], mc, api_key),
                lambda coll, err: _install_resolved(r, coll, err))

        def _install_resolved(r, coll, err):
            if not dlg.winfo_exists():
                return
            if err is not None:
                busy(f"解決失敗: {err}")
                messagebox.showerror("導入エラー", str(err), parent=dlg)
                return
            warns = coll.pop("__warnings__", [])
            entries = list(coll.values())
            lines = "\n".join(f"・{e['name']}  {e['version']}" for e in entries)
            msg = (f"{r['name']} を依存込みで {len(entries)} 個 導入し、"
                   f"サーバーを再起動します。\n\n{lines}")
            if warns:
                msg += "\n\n⚠ 一部の依存はスキップ:\n" + "\n".join(f"・{w}" for w in warns)
            if not messagebox.askyesno("Mod導入", msg, parent=dlg):
                busy("")
                return
            busy("ダウンロードして導入中…")
            self._task_submit(
                f"🧩 Mod導入: {r['name']} ({p.display_name})",
                lambda: modmanager.install_online(
                    p, entries, restart=True, progress=self._progress_from_worker),
                lambda _r, e2: (busy("導入完了" if not e2 else f"失敗: {e2}"),
                                refresh_installed()),
                category="Mod管理", busy=False)

        ttk.Button(sb, text="🔍 検索", command=do_search).pack(side=tk.LEFT, padx=6)
        q_entry.bind("<Return>", lambda _e: do_search())
        ttk.Button(src_lf, text="⬇ 選択を導入(依存も自動)", command=install_selected
                   ).pack(anchor=tk.W, padx=6, pady=(0, 8))

        refresh_installed()

    def _sv_server_config(self) -> None:
        """サーバーの詳細設定(Minecraftはserver.properties)を編集するダイアログ。"""
        server = self._selected_server()
        if server is None:
            return
        p = server.profile
        if p.game == "palworld":
            self._pal_server_config(server)
            return
        if p.game != "minecraft":
            messagebox.showinfo(
                "未対応", "詳細設定はMinecraft / Palworldに対応しています。")
            return
        self._begin_busy(f"{p.display_name}: 設定を読み込み中…")

        def on_done(text, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"設定読込エラー: {error}")
                messagebox.showerror("読込エラー", str(error))
                return
            self._open_server_config_dialog(server, text)

        self._submit(lambda: serverconfig.read_config(p), on_done)

    def _open_server_config_dialog(self, server, text: str) -> None:
        p = server.profile
        props = serverconfig.Properties(text)

        dialog = tk.Toplevel(self)
        dialog.title(f"詳細設定: {p.display_name}  ({p.config_file})")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("620x640+%d+%d"
                        % (self.winfo_rootx() + 160, self.winfo_rooty() + 60))

        # --- スクロール領域 ---
        outer = ttk.Frame(dialog, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0, bg=PAL["bg"])
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        def close() -> None:
            canvas.unbind_all("<MouseWheel>")
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close)

        getters: dict[str, object] = {}   # key -> 値取得callable

        # --- よく使う設定(整形) ---
        curated_keys = set()
        cf = ttk.LabelFrame(body, text="よく使う設定")
        cf.pack(fill=tk.X, padx=4, pady=(0, 8))
        row = 0
        for key, kind, choices in MC_PROPERTIES_CURATED:
            if key not in props.keys():
                continue
            curated_keys.add(key)
            self._cfg_label_cell(cf, row, key)
            cur = props.get(key)
            if kind == "bool":
                var = tk.BooleanVar(value=(cur.strip().lower() == "true"))
                ttk.Checkbutton(cf, variable=var).grid(
                    row=row, column=1, sticky=tk.W, padx=6)
                getters[key] = lambda v=var: "true" if v.get() else "false"
            elif kind == "choice":
                jp_by_val = {v: j for v, j in choices}
                val_by_jp = {j: v for v, j in choices}
                cur_v = cur.strip()
                disp = [j for _, j in choices]
                cur_jp = jp_by_val.get(cur_v)
                if cur_jp is None:          # 未知の値はそのまま保持
                    disp = [cur_v] + disp
                    val_by_jp[cur_v] = cur_v
                    cur_jp = cur_v
                var = tk.StringVar(value=cur_jp)
                ttk.Combobox(cf, textvariable=var, values=disp,
                             state="readonly", width=20).grid(
                    row=row, column=1, sticky=tk.W, padx=6)
                getters[key] = lambda v=var, m=val_by_jp: m.get(v.get(), v.get())
            else:
                var = tk.StringVar(value=cur)
                ttk.Entry(cf, textvariable=var, width=32).grid(
                    row=row, column=1, sticky=tk.W, padx=6)
                getters[key] = lambda v=var: v.get()
            row += 1

        # --- その他(生値) ---
        others = [k for k in props.keys() if k not in curated_keys]
        if others:
            of = ttk.LabelFrame(body, text="その他の設定(生値)")
            of.pack(fill=tk.X, padx=4, pady=(0, 8))
            for i, key in enumerate(others):
                self._cfg_label_cell(of, i, key)
                var = tk.StringVar(value=props.get(key))
                ttk.Entry(of, textvariable=var, width=32).grid(
                    row=i, column=1, sticky=tk.W, padx=6)
                getters[key] = lambda v=var: v.get()

        # --- 保存/キャンセル ---
        btns = ttk.Frame(dialog, padding=(8, 6))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        restart_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(btns, text="保存後に再起動",
                        variable=restart_var).pack(side=tk.LEFT)

        def save() -> None:
            for key, getter in getters.items():
                props.set(key, str(getter()))
            new_text = props.text()
            restart = restart_var.get()
            if restart and not messagebox.askyesno(
                    "確認",
                    f"{p.display_name} の設定を保存して再起動します。\n"
                    "接続中のプレイヤーは一度切断されます。続行しますか?",
                    parent=dialog):
                return
            close()
            self._begin_busy(f"{p.display_name}: 設定を保存中…")

            def on_done(_r, error) -> None:
                self._end_busy()
                if error is not None:
                    self._set_status(f"保存エラー: {error}")
                    messagebox.showerror("保存エラー", str(error))
                else:
                    extra = "(再起動済み)" if restart else ""
                    self._set_status(f"{p.display_name}: 設定を保存しました{extra}")
                    self._append_log(
                        f"■ {p.display_name}: {p.config_file} を更新しました{extra}")

            title = f"{p.display_name}: 詳細設定を保存"
            if restart:
                title += "+再起動"
            self._task_submit(
                title,
                lambda: serverconfig.write_config(
                    p, new_text, restart=restart,
                    progress=self._progress_from_worker), on_done,
                category="設定変更", busy=False)

        ttk.Button(btns, text="💾 保存", command=save).pack(side=tk.RIGHT)
        ttk.Button(btns, text="キャンセル", command=close).pack(
            side=tk.RIGHT, padx=6)

    def _auto_profile_id(self, base: str) -> str:
        """プロファイルIDを自動生成する。英小文字数字_のみ・既存と衝突しないよう調整。"""
        import re
        slug = re.sub(r"[^a-z0-9_]", "", base.lower().replace("-", "_")) or "server"
        if slug not in self.servers:
            return slug
        n = 2
        while f"{slug}{n}" in self.servers:
            n += 1
        return f"{slug}{n}"

    def _assign_external_port(self, profile) -> int:
        """このサーバーの外部ポートを決める。未割当なら空きを自動採番して永続化。"""
        if profile.external_port:
            return profile.external_port
        used = {s.profile.external_port for s in self.servers.values()
                if s.profile.external_port}
        port = profile.game_port or 25565
        while port in used:
            port += 1
        settings.update_config(CONFIG_PATH, {"servers": {profile.name: {"external_port": port}}})
        profile.external_port = port
        return port

    def _sv_publish(self) -> None:
        """選択サーバーを名前(FQDN)で外部公開する(A + SRV + UPnP)。"""
        server = self._selected_server()
        if server is None:
            return
        p = server.profile
        if not p.game_port:
            messagebox.showerror("ポート不明", "このサーバーはgame_portが未設定です")
            return
        if p.game == "palworld":                 # Palworldは UDP + SRV非対応。専用処理へ
            self._pal_publish(server)
            return
        if not p.fqdn or self.config_data.dns is None or self.config_data.publish is None:
            messagebox.showerror(
                "設定不足",
                "名前公開には fqdn / dns / publish 設定が必要です。\n"
                "config.yamlを確認してください。")
            return
        ext_port = self._assign_external_port(p)
        if not messagebox.askyesno(
                "外部公開の確認",
                f"「{p.display_name}」を名前(FQDN)で公開します。\n\n"
                f"  接続名 : {p.fqdn}(ポート入力不要・SRVで自動)\n"
                f"  経路   : WAN:{ext_port} → {p.address}:{p.game_port}\n\n"
                "世界中から接続可能になります。ホワイトリスト運用を推奨します。\n"
                "続行しますか?"):
            return
        self._begin_busy(f"{p.display_name} を外部公開中…")

        def job() -> str:
            gw = upnp.find_gateway(
                bind_ip=upnp.local_ip_toward(self.config_data.network.gateway),
                prefer_host=self.config_data.network.gateway)
            wan = gw.external_ip
            # 1. ルーター: WAN:ext_port → address:game_port
            upnp.add_mapping(gw, ext_port, p.address, p.game_port, "TCP",
                             description=f"gsm-{p.name}")
            m = upnp.get_mapping(gw, ext_port, "TCP")
            if m is None or m["internal_ip"] != p.address:
                raise RuntimeError("ルーターのマッピング確認に失敗しました")
            # 2. DNS: A(fqdn→WAN) + SRV(ポート隠蔽)
            dnsreg.publish_server(self.config_data.dns, p.fqdn, wan, ext_port,
                                  service="minecraft", progress=self._progress_from_worker)
            return wan

        def on_done(wan, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"外部公開エラー: {error}")
                messagebox.showerror("外部公開エラー", str(error))
                return
            self._append_log(
                f"■ {p.display_name} を名前で外部公開しました\n"
                f"  接続名(友だちに教える): {p.fqdn}\n"
                f"  ※ ポート入力は不要(SRVレコードで {ext_port} に自動振り分け)\n"
                f"  WAN IP: {wan} / 経路: WAN:{ext_port} → {p.address}:{p.game_port}\n"
                f"  ホワイトリスト: RCONコンソールで whitelist add <名前>")
            self._set_status(f"{p.display_name} を公開: {p.fqdn}")

        self._task_submit(f"{p.display_name} を外部公開({p.fqdn})",
                          job, on_done, category="外部公開", busy=False)

    def _sv_unpublish(self) -> None:
        server = self._selected_server()
        if server is None:
            return
        p = server.profile
        ext_port = p.external_port or p.game_port
        if not ext_port:
            return
        self._begin_busy(f"{p.display_name} の公開を停止中…")

        def job() -> None:
            gw = upnp.find_gateway(
                bind_ip=upnp.local_ip_toward(self.config_data.network.gateway),
                prefer_host=self.config_data.network.gateway)
            upnp.delete_mapping(gw, ext_port, "TCP")
            if p.fqdn and self.config_data.dns is not None:
                dnsreg.unpublish_server(self.config_data.dns, p.fqdn,
                                        service="minecraft",
                                        progress=self._progress_from_worker)

        def on_done(_r, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"公開停止エラー: {error}")
                messagebox.showerror("公開停止エラー", str(error))
                return
            self._set_status(f"{p.display_name} の外部公開を停止しました")

        self._task_submit(f"{p.display_name} の外部公開を停止",
                          job, on_done, category="公開停止", busy=False)

    def _sv_conntest(self) -> None:
        """外部プレイヤー視点(公開DNS 8.8.8.8経由)で名前だけ接続を試し、到達先を表示。"""
        server = self._selected_server()
        if server is None:
            return
        p = server.profile
        if not p.fqdn:
            messagebox.showinfo("FQDN未設定", "このサーバーはfqdnが未設定です")
            return
        if p.game == "palworld":
            self._pal_conntest(server)
            return
        self._begin_busy(f"{p.fqdn} を外部視点で接続テスト中…")

        def on_done(r, error) -> None:
            self._end_busy()
            if error is not None:
                self._append_log(f"接続テスト失敗: {error}")
                self._set_status("接続テスト失敗")
                return
            hit = "到達" if r.online else "到達できず"
            match = ""
            if r.online:
                # MOTDやプレイヤー数で、意図したサーバーに届いたかの目安
                match = ("  ← このサーバーに届いています"
                         if p.display_name and r.motd else "")
            lines = [
                f"■ 接続テスト(外部プレイヤー視点 / リゾルバ {r.resolver})",
                f"  接続名     : {p.fqdn}(ポート入力なし)",
                f"  SRV使用    : {'はい(ポート自動)' if r.used_srv else 'いいえ(25565既定)'}",
                f"  実際の接続先: {r.endpoint}",
                f"  結果       : {hit}",
            ]
            if r.online:
                lines += [
                    f"  届いたMOTD : {r.motd}{match}",
                    f"  バージョン : {r.version} / プレイヤー: {r.players}",
                    "",
                    "※ これは世界の公開DNS経由の結果です。友だちが名前だけで繋いだ時と同じ。",
                ]
            else:
                lines += [f"  エラー     : {r.error}",
                          "※ ポート開放(🌍外部公開)が済んでいるか確認してください。"]
            self._append_log("\n".join(lines))
            self._set_status(f"接続テスト完了: {p.fqdn} → "
                             f"{'到達' if r.online else '到達できず'}")

        self._submit(lambda: conntest.test_server(p.fqdn, resolver="8.8.8.8"), on_done)

    def _pal_conntest(self, server) -> None:
        """Palworld(UDP)の外部公開チェック: サーバー応答 / DNS→WAN / ポート開放を確認。"""
        p = server.profile
        pub = self.config_data.publish.public_name if self.config_data.publish else None
        self._begin_busy("Palworld 外部公開を確認中…")

        def job():
            import subprocess
            import re
            try:                                  # サーバーが起動＆RCON応答するか
                local = (server.status() == "active"
                         and bool(server.rcon_command("Info")))
            except Exception:
                local = False
            gw = upnp.find_gateway(
                bind_ip=upnp.local_ip_toward(self.config_data.network.gateway),
                prefer_host=self.config_data.network.gateway)
            wan = gw.external_ip
            fwd = upnp.get_mapping(gw, p.game_port, "UDP")

            def resolve(name):
                r = subprocess.run(["nslookup", name, "8.8.8.8"],
                                   capture_output=True, text=True)
                ips = re.findall(r"Address:\s*([\d.]+)", r.stdout)
                return ips[-1] if ips else None
            names = {p.fqdn: resolve(p.fqdn)}
            if pub:
                names[pub] = resolve(pub)
            return {"local": local, "wan": wan, "fwd": fwd, "names": names,
                    "port": p.game_port}

        def on_done(res, error):
            self._end_busy()
            if error is not None:
                self._append_log(f"Palworld外部チェック失敗: {error}")
                self._set_status("Palworld外部チェック失敗")
                return
            fwd = res["fwd"]
            fwd_ok = bool(fwd and str(fwd.get("internal_ip")) == p.address)
            dns_ok = any(ip == res["wan"] for ip in res["names"].values())
            ok = res["local"] and fwd_ok and dns_ok
            verdict = "✅ 外部公開OK(友だちが接続できる状態)" if ok else "⚠ 要確認"
            lines = [
                "■ Palworld 外部公開チェック(UDP)",
                f"  サーバー稼働(RCON応答): "
                f"{'OK' if res['local'] else '応答なし(起動直後/停止中?)'}",
            ]
            for name, ip in res["names"].items():
                lines.append(f"  DNS {name} → {ip or '解決不可'} "
                             f"(WAN {res['wan']}): {'一致' if ip == res['wan'] else '不一致'}")
            lines += [
                f"  ポート開放 {res['port']}/UDP → "
                f"{fwd.get('internal_ip') if fwd else '未開放'}: {'OK' if fwd_ok else 'NG'}",
                f"  判定: {verdict}",
                "",
                f"  友だちの接続先: {(pub or p.fqdn)}:{res['port']}",
                "  ※ PalworldはUDP＆ポート必須。TCP接続テストは使えないためコンポーネントで判定。",
            ]
            self._append_log("\n".join(lines))
            self._set_status(f"Palworld外部チェック: {verdict}")

        self._submit(job, on_done)

    def _pal_publish(self, server) -> None:
        """Palworldを外部公開: game_port/UDP をWANに開放 ＋ FQDNのAレコードをWANへ(SRVなし)。"""
        p = server.profile
        if self.config_data.dns is None:
            messagebox.showerror("設定不足", "名前公開には dns 設定が必要です。")
            return
        pubname = p.fqdn
        if not messagebox.askyesno(
                "外部公開(Palworld)",
                f"{p.display_name} を外部公開します。\n"
                f"・{p.game_port}/UDP をWANに開放\n"
                f"・{pubname} を WAN IP に登録(PalworldはSRV非対応)\n\n"
                f"友だちは {pubname}:{p.game_port} で接続します。続行しますか?"):
            return
        self._begin_busy(f"{p.display_name} を外部公開中…")

        def job():
            gw = upnp.find_gateway(
                bind_ip=upnp.local_ip_toward(self.config_data.network.gateway),
                prefer_host=self.config_data.network.gateway)
            wan = gw.external_ip
            if upnp.get_mapping(gw, p.game_port, "UDP") is None:
                upnp.add_mapping(gw, p.game_port, p.address, p.game_port, "UDP",
                                 description=f"gsm-{p.name}")
            dnsreg.set_a_record(self.config_data.dns, pubname, wan,
                                progress=self._progress_from_worker)
            return wan

        def on_done(wan, error):
            self._end_busy()
            if error is not None:
                self._set_status(f"外部公開エラー: {error}")
                messagebox.showerror("外部公開エラー", str(error))
                return
            self._append_log(
                f"■ {p.display_name} を外部公開しました(Palworld/UDP)\n"
                f"  接続名: {pubname}:{p.game_port}\n"
                f"  WAN IP: {wan} / {p.game_port}/UDP → {p.address}\n"
                f"  ※ ポートは必須。SRVは使いません。")
            self._set_status(f"{p.display_name} を公開: {pubname}:{p.game_port}")

        self._submit(job, on_done)

    def _sv_log(self) -> None:
        server = self._selected_server()
        if server is None:
            return
        self._begin_busy("ログを取得中…")

        def on_done(result, error) -> None:
            self._end_busy()
            self._append_log(str(error) if error is not None else result)
            self._set_status("完了")

        self._submit(lambda: server.tail_log(100), on_done)

    def _rcon_send(self) -> None:
        server = self._selected_server()
        if server is None:
            return
        cmd = self.rcon_entry.get().strip()
        if not cmd:
            return
        self._begin_busy(f"RCON: {cmd}")

        def on_done(result, error) -> None:
            self._end_busy()
            body = str(error) if error is not None else (result.strip() or "(応答なし)")
            self._append_log(f"> {cmd}\n{body}")
            self._set_status("完了")

        self.rcon_entry.delete(0, tk.END)
        self._submit(lambda: server.rcon_command(cmd), on_done)

    def _sv_quick(self, cmd: str) -> None:
        """MCのクイックコマンド(RCONで即実行)。"""
        server = self._selected_server()
        if server is None:
            return
        if cmd == "__say__":
            msg = simpledialog.askstring("全体メッセージ", "送る内容:", parent=self)
            if not msg:
                return
            cmd = "say " + msg
        self._begin_busy(f"RCON: {cmd}")

        def on_done(result, error) -> None:
            self._end_busy()
            body = str(error) if error is not None else (result.strip() or "(応答なし)")
            self._append_log(f"> {cmd}\n{body}")
            self._set_status(f"{server.profile.display_name}: {cmd}")

        self._submit(lambda: server.rcon_command(cmd), on_done)

    # ---------- 新規サーバー構築 ----------

    def _sv_provision(self) -> None:
        templates = provision.load_templates()
        if not templates:
            messagebox.showerror("エラー", "provisioners/ にテンプレートがありません")
            return

        dialog = tk.Toplevel(self)
        dialog.title("新規サーバー構築")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 150, self.winfo_rooty() + 60))
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill=tk.BOTH, expand=True)

        # 既存プロファイルからSSH既定値を拝借(全VM共通運用のため)
        first = next(iter(self.servers.values()), None)
        default_ssh_user = first.profile.ssh_user if first else "ubuntu"
        default_ssh_pass = (first.profile.ssh_password or "") if first else ""
        vm_names = [self.vm_tree.item(i, "text") for i in self.vm_tree.get_children()]

        # --- モード選択 ---
        mode_var = tk.StringVar(value="clone")
        mode_frame = ttk.LabelFrame(form, text="構築モード")
        mode_frame.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))
        ttk.Radiobutton(mode_frame, text="テンプレートからクローンして新規VM作成(推奨)",
                        variable=mode_var, value="clone").pack(anchor=tk.W, padx=6)
        ttk.Radiobutton(mode_frame, text="既存のUbuntu VMに構築",
                        variable=mode_var, value="existing").pack(anchor=tk.W, padx=6)

        net = self.config_data.network
        tmpl_by_label = {t.label: t for t in templates}
        rows = [
            ("ゲーム種別", "template", list(tmpl_by_label.keys())[0], list(tmpl_by_label.keys())),
            ("バージョン(例 1.20.1 / 1.12.2)", "mc_version", templates[0].mc_version, None),
            ("プロファイルID(空欄で自動)", "name", "", None),
            ("表示名", "display_name", "", None),
            ("SSHユーザー", "ssh_user", default_ssh_user, None),
            ("SSHパスワード", "ssh_password", default_ssh_pass, None),
            # --- クローンモード用 ---
            ("テンプレVM名", "clone_template_vm", "ubuntu_template", vm_names),
            ("テンプレVMのIP", "clone_template_ip", "192.168.11.199", None),
            ("新VM名", "clone_vm_name", "", None),
            ("新ホスト名", "clone_hostname", "", None),
            (f"新IP({net.prefix}.X のX。空きから選択)", "clone_new_ip", "", []),
            ("メモリ(MB)", "clone_memory", "3072", None),
            ("CPU数", "clone_cpu", "1", None),
            # --- 既存VMモード用 ---
            ("既存VMのIP(第4オクテットのみ可)", "address", "", None),
            ("既存VMのHyper-V名", "vm", "", vm_names),
        ]
        vars_: dict[str, tk.StringVar] = {}
        widgets: dict[str, ttk.Widget] = {}
        for i, (label, key, default, choices) in enumerate(rows, start=1):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky=tk.W, pady=2)
            var = tk.StringVar(value=default)
            vars_[key] = var
            if choices is not None:
                w = ttk.Combobox(form, textvariable=var, values=choices, width=32)
            else:
                w = ttk.Entry(form, textvariable=var, width=34,
                              show="*" if key == "ssh_password" else "")
            w.grid(row=i, column=1, sticky=tk.W, pady=2, padx=(8, 0))
            widgets[key] = w

        # ゲーム種別を変えたら既定バージョンを切り替える
        def _on_tmpl_change(_e=None) -> None:
            t = tmpl_by_label.get(vars_["template"].get())
            if t:
                vars_["mc_version"].set(t.mc_version)
        widgets["template"].bind("<<ComboboxSelected>>", _on_tmpl_change)

        note = "※ DNS(A/PTR)は自動登録、RCONパスワードは自動生成" if self.config_data.dns \
            else "※ RCONパスワードは自動生成"
        scan_var = tk.StringVar(value=f"空きIPを検索中… ({net.subnet_text} の "
                                      f"{net.vm_range[0]}〜{net.vm_range[1]})")
        ttk.Label(form, textvariable=scan_var, foreground="#777777").grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Label(form, text=note, foreground="#777777").grid(
            row=len(rows) + 2, column=0, columnspan=2, sticky=tk.W)

        # 空きIPスキャン(裏で実行してコンボボックスに反映)
        reserved = {p.address for s in self.servers.values() for p in [s.profile]}

        def on_scanned(used, error) -> None:
            if not dialog.winfo_exists():
                return
            if error is not None:
                scan_var.set(f"空きIPスキャン失敗: {error}")
                return
            free = [str(o) for o in range(net.vm_range[0], net.vm_range[1] + 1)
                    if o not in used and f"{net.prefix}.{o}" not in reserved]
            widgets["clone_new_ip"]["values"] = free
            if free and not vars_["clone_new_ip"].get():
                vars_["clone_new_ip"].set(free[0])
            scan_var.set(f"空きIP: {len(free)}個 (使用中を除外済み。手入力も可)")

        self._submit(lambda: netscan.scan_used_octets(net.prefix, *net.vm_range),
                     on_scanned)

        def submit() -> None:
            tmpl = tmpl_by_label[vars_["template"].get()]
            values = {k: v.get().strip() for k, v in vars_.items()}
            values["mode"] = mode_var.get()
            if not values["ssh_password"]:
                messagebox.showerror("入力不足", "SSHパスワードは必須です", parent=dialog)
                return
            # プロファイルIDが空なら自動生成(ホスト名優先→VM名→ゲームID)
            if not values["name"]:
                base = (values.get("clone_hostname") or values.get("clone_vm_name")
                        or values.get("vm") or tmpl.id)
                values["name"] = self._auto_profile_id(base)
            try:
                if values["mode"] == "clone":
                    need = ("clone_template_vm", "clone_template_ip", "clone_vm_name",
                            "clone_hostname", "clone_new_ip")
                    if not all(values[k] for k in need):
                        messagebox.showerror(
                            "入力不足", "テンプレVM・新VM名・ホスト名・新IPを入力してください",
                            parent=dialog)
                        return
                    values["clone_new_ip"] = net.full_ip(values["clone_new_ip"])
                else:
                    if not values["address"]:
                        messagebox.showerror("入力不足", "既存VMのIPアドレスを入力してください",
                                             parent=dialog)
                        return
                    values["address"] = net.full_ip(values["address"])
            except ValueError as exc:
                messagebox.showerror("入力エラー", str(exc), parent=dialog)
                return
            dialog.destroy()
            self._run_provision(tmpl, values)

        btns = ttk.Frame(form)
        btns.grid(row=len(rows) + 2, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="構築開始", command=submit).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT)

    def _run_provision(self, tmpl, values: dict) -> None:
        name = values["name"]
        rcon_password = provision.generate_password()
        params = dict(tmpl.defaults)
        params["rcon_password"] = rcon_password
        params["ssh_user"] = values["ssh_user"]
        if values.get("mc_version"):        # ウィザードで指定した版で上書き
            params["mc_version"] = values["mc_version"]
        script = provision.render_script(tmpl, params)
        display_name = values["display_name"] or name
        clone_mode = values.get("mode") == "clone"

        if clone_mode:
            values["address"] = values["clone_new_ip"]
            values["vm"] = values["clone_vm_name"]
            # DNS自動登録が有効ならFQDNも決まる
            if self.config_data.dns:
                values["fqdn"] = (f"{values['clone_hostname']}."
                                  f"{self.config_data.dns.domain}")
        values.setdefault("fqdn", "")

        self._begin_busy(f"{display_name} を構築中…(数分かかります)")
        log_lines: list[str] = []

        def on_line(line: str) -> None:
            log_lines.append(line)
            if line.strip():
                self._progress_from_worker(f"構築中: {line.strip()[:70]}")

        def job() -> str:
            if clone_mode:
                # IP競合の最終チェック(ping + SSHポート)
                self._progress_from_worker(f"IP競合チェック中: {values['clone_new_ip']}")
                if netscan.ip_in_use(values["clone_new_ip"]):
                    raise RuntimeError(
                        f"{values['clone_new_ip']} は既に使用中です(ping/SSH応答あり)。"
                        "別のIPを指定してください")
                on_line(f"== VMクローン: {values['clone_template_vm']} → "
                        f"{values['clone_vm_name']} ==")
                self._progress_from_worker("VMディスクを複製中…(数分かかります)")
                self.hyperv.clone_vm(
                    values["clone_template_vm"], values["clone_vm_name"],
                    int(values["clone_memory"]), int(values["clone_cpu"]))
                on_line("== クローン完了。起動を待機 ==")
                from core.orchestration import _wait_for_port
                _wait_for_port(values["clone_template_ip"], 22, 240)
                on_line("== 個体化(ホスト名/machine-id/SSH鍵/新IP) ==")
                gateway = self.config_data.network.gateway
                dns_ip = self.config_data.dns.host if self.config_data.dns \
                    else "192.168.11.254"
                individualize_clone(
                    values["clone_template_ip"], values["ssh_user"],
                    values["ssh_password"], values["clone_hostname"],
                    values["clone_new_ip"], gateway, dns_ip,
                    progress=self._progress_from_worker)
                on_line(f"== 個体化完了({values['clone_new_ip']}) ==")
            if self.config_data.dns and values.get("fqdn"):
                dnsreg.register_host(
                    self.config_data.dns,
                    values.get("clone_hostname") or values["fqdn"],
                    values["address"], progress=self._progress_from_worker)
                on_line(f"== DNS登録完了: {values['fqdn']} ==")
            on_line("== ゲームサーバー構築開始 ==")
            return provision.provision(
                values["address"], values["ssh_user"], values["ssh_password"],
                script, progress=on_line)

        def on_done(_log, error) -> None:
            self._end_busy()
            self._append_log("\n".join(log_lines) or "(出力なし)")
            if error is not None:
                self._set_status(f"構築失敗: {error}")
                messagebox.showerror("構築失敗", str(error))
                return
            # config.yamlへプロファイル追記
            profile: dict = {
                "display_name": display_name,
                "vm": values["vm"] or None,
                "address": values["address"],
                "ssh": {"user": values["ssh_user"], "password": values["ssh_password"]},
                "service": params["service"],
                "rcon": {"port": int(params["rcon_port"]), "password": rcon_password},
                "game_port": int(params["game_port"]),
            }
            if values["fqdn"]:
                profile["fqdn"] = values["fqdn"]
            profile.update(tmpl.profile_extra)
            try:
                provision.append_profile_to_config(CONFIG_PATH, name, profile)
            except Exception as exc:
                messagebox.showerror(
                    "構築は成功、config追記に失敗",
                    f"サーバー自体は構築できています。config.yamlに手動で追加してください。\n{exc}")
                return
            # 実行中のアプリにも反映
            cfg = load_config(CONFIG_PATH)
            new_profile = next(p for p in cfg.servers if p.name == name)
            self.servers[name] = GameServer(new_profile)
            addr, port = self._server_addr_port(new_profile)
            if not self.sv_tree.exists(name):
                pub = "…" if getattr(new_profile, "external_port", None) else "―"
                self.sv_tree.insert("", tk.END, iid=name, text=new_profile.display_name,
                                    values=(new_profile.vm or "-", "…", pub, addr, port, "?", ""))
            self._set_status(f"{display_name} の構築が完了しました")
            messagebox.showinfo("構築完了",
                                f"{display_name} の構築が完了しました。\n"
                                f"config.yamlにも登録済みです。")
            self.refresh_all()

        self._task_submit(f"サーバー構築: {display_name}",
                          job, on_done, category="サーバー構築", busy=False)

    # ---------- SQL共有タブ ----------

    def _sql_refresh(self) -> None:
        self._begin_busy("SQLグループを取得中…")

        def on_done(groups, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"MySQLエラー: {error}")
                self._set_sql_text(str(error))
                return
            self.sql_tree.delete(*self.sql_tree.get_children())
            addr_to_name = {s.profile.address: s.profile.display_name
                            for s in self.servers.values()}
            for g in groups:
                members = ", ".join(
                    f"{ip}({addr_to_name[ip]})" if ip in addr_to_name else ip
                    for ip in g.members) or "(なし)"
                self.sql_tree.insert("", tk.END, iid=g.name, text=g.name,
                                     values=(g.database, members))
            self._set_status("SQLグループ更新完了")

        self._submit(self.sqlshare.list_groups, on_done)

    def _sql_create_group(self) -> None:
        name = simpledialog.askstring(
            "グループ作成",
            "グループ名(英小文字・数字・_、24文字以内):\n例: mc_network",
            parent=self)
        if not name:
            return
        self._begin_busy(f"グループ {name} を作成中…")

        def on_done(_result, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"エラー: {error}")
                messagebox.showerror("エラー", str(error))
            else:
                self._set_status(f"グループ {name} 作成完了")
            self._sql_refresh()

        self._task_submit(f"SQL共有グループ作成: {name}",
                          lambda: self.sqlshare.create_group(name), on_done,
                          category="SQL共有", busy=False)

    def _sql_delete_group(self) -> None:
        group = self._selected_group()
        if group is None:
            return
        if not messagebox.askyesno(
                "確認",
                f"グループ「{group}」を削除しますか?\n\n"
                "共有データベースの中身も完全に削除されます。",
                icon="warning", default="no"):
            return
        self._begin_busy(f"グループ {group} を削除中…")

        def job() -> None:
            # 削除前に各メンバーからMODを撤去(再起動を伴う)
            if self._mod_sync_active():
                info = self.sqlshare.connection_info(group)
                for mip in info.get("members", []):
                    server = self._server_by_address(mip)
                    if server is not None:
                        moddeploy.uninstall(server.profile, self.config_data.mod_sync,
                                            progress=self._progress_from_worker)
            self.sqlshare.delete_group(group)

        def on_done(_result, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"エラー: {error}")
                messagebox.showerror("エラー", str(error))
            else:
                self._set_status(f"グループ {group} 削除完了")
            self._sql_refresh()

        self._task_submit(f"SQL共有グループ削除: {group}",
                          job, on_done, category="SQL共有", busy=False)

    def _server_by_address(self, ip: str):
        """IPアドレスから GameServer を引く(MOD配布のプロファイル取得用)。"""
        for s in self.servers.values():
            if s.profile.address == ip:
                return s
        return None

    def _mod_sync_active(self) -> bool:
        ms = self.config_data.mod_sync
        return ms is not None and ms.enabled

    def _choose_server_dialog(self, title: str) -> str | None:
        """設定済みサーバーから1つ選ぶ簡易ダイアログ。IPアドレスを返す。"""
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 200, self.winfo_rooty() + 200))
        ttk.Label(dialog, text="サーバーを選択:").pack(padx=12, pady=(12, 4))
        choices = {f"{s.profile.display_name} ({s.profile.address})": s.profile.address
                   for s in self.servers.values()}
        var = tk.StringVar()
        combo = ttk.Combobox(dialog, textvariable=var,
                             values=list(choices.keys()), state="readonly", width=40)
        combo.pack(padx=12, pady=4)
        if choices:
            combo.current(0)
        result: list[str | None] = [None]

        def ok() -> None:
            result[0] = choices.get(var.get())
            dialog.destroy()

        btns = ttk.Frame(dialog)
        btns.pack(pady=12)
        ttk.Button(btns, text="OK", command=ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT)
        dialog.wait_window()
        return result[0]

    def _sql_add_server(self) -> None:
        group = self._selected_group()
        if group is None:
            return
        ip = self._choose_server_dialog(f"グループ {group} にサーバー追加")
        if ip is None:
            return
        server = self._server_by_address(ip)
        do_mod = self._mod_sync_active() and server is not None
        if do_mod and not messagebox.askyesno(
                "確認",
                f"{server.profile.display_name} を「{group}」に追加します。\n\n"
                "同期MOD(invsyncmod + fabric-api)を導入してサーバーを再起動します。\n"
                "接続中のプレイヤーは一度切断されます。続行しますか?"):
            return
        self._begin_busy(f"{ip} を {group} に追加中…")

        def job() -> None:
            self.sqlshare.add_server(group, ip)
            if do_mod:
                info = self.sqlshare.connection_info(group)
                moddeploy.install(server.profile, info, self.config_data.mod_sync,
                                  CONFIG_PATH.parent, progress=self._progress_from_worker)

        def on_done(_result, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"エラー: {error}")
                messagebox.showerror("エラー", str(error))
            else:
                extra = "(MOD導入+再起動済み)" if do_mod else ""
                self._set_status(f"{ip} を {group} に追加完了 {extra}")
            self._sql_refresh()

        self._task_submit(f"SQL共有: {ip} を {group} に追加",
                          job, on_done, category="SQL共有", busy=False)

    def _sql_remove_server(self) -> None:
        group = self._selected_group()
        if group is None:
            return
        ip = self._choose_server_dialog(f"グループ {group} からサーバー除外")
        if ip is None:
            return
        server = self._server_by_address(ip)
        do_mod = self._mod_sync_active() and server is not None
        self._begin_busy(f"{ip} を {group} から除外中…")

        def job() -> None:
            if do_mod:
                moddeploy.uninstall(server.profile, self.config_data.mod_sync,
                                    progress=self._progress_from_worker)
            self.sqlshare.remove_server(group, ip)

        def on_done(_result, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"エラー: {error}")
                messagebox.showerror("エラー", str(error))
            else:
                extra = "(MOD削除+再起動済み)" if do_mod else ""
                self._set_status(f"{ip} を {group} から除外完了 {extra}")
            self._sql_refresh()

        self._task_submit(f"SQL共有: {ip} を {group} から除外",
                          job, on_done, category="SQL共有", busy=False)

    def _sql_show_info(self) -> None:
        group = self._selected_group()
        if group is None:
            return
        self._begin_busy("接続情報を取得中…")

        def on_done(info, error) -> None:
            self._end_busy()
            if error is not None:
                self._set_status(f"エラー: {error}")
                self._set_sql_text(str(error))
                return
            addr_to_name = {s.profile.address: s.profile.display_name
                            for s in self.servers.values()}
            members = "\n".join(
                f"    - {ip}" + (f" ({addr_to_name[ip]})" if ip in addr_to_name else "")
                for ip in info["members"]) or "    (なし)"
            self._set_sql_text(
                f"■ グループ「{group}」の接続情報(プラグイン設定用)\n"
                f"  ホスト      : {info['host']}\n"
                f"  ポート      : {info['port']}\n"
                f"  データベース: {info['database']}\n"
                f"  ユーザー    : {info['user']}\n"
                f"  パスワード  : {info['password']}\n"
                f"  JDBC URL    : jdbc:mysql://{info['host']}:{info['port']}/{info['database']}\n"
                f"  参加サーバー:\n{members}\n\n"
                f"  ※ 参加サーバーのIPからのみ接続できます。")
            self._set_status("完了")

        self._submit(lambda: self.sqlshare.connection_info(group), on_done)

    # ---------- 終了 ----------

    def _on_close(self) -> None:
        self._resmon_stop = True
        self._pubstat_stop = True
        for server in self.servers.values():
            server.close()
        try:
            self.dynserver.stop()
        except Exception:
            pass
        self.destroy()
