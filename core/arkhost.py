"""ホストで動くARK(ASA)サーバーの管理。

ARKはVM内ではなくホスト(このマシン)で動くため、VM/SSHの仕組みは使わず、
ホストのプロセス操作(PowerShell)とRCONで扱う。
  - 状態  : ArkAscendedServer.exe プロセスの有無
  - 情報  : セッション名(ini)+ プレイヤー(RCON ListPlayers)
  - 起動  : exe + 引数で起動(二重起動ガード)
  - 停止  : RCON saveworld -> DoExit(保存して終了)、応答なければプロセス停止
RCONポート/管理パスワードは config_dir の GameUserSettings.ini から取得する
(ASASMのGUIで変えても自動で追従する)。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .rcon import RconClient, RconError


@dataclass
class ArkHostConfig:
    display_name: str = "ARK (ホスト)"
    exe_path: str = ""
    launch_args: str = ""        # mapは"..."で括った完成形(例 '"TheIsland_WP?Port=7777..." -ServerPlatform=ALL')
    process_name: str = "ArkAscendedServer"
    config_dir: str = ""         # GameUserSettings.ini のあるフォルダ
    rcon_host: str = "127.0.0.1"
    install_dir: str = ""        # このマップ専用のインストール(空=exe_pathから導出)
    use_dynamic_config: bool = False  # ON時、起動引数に -UseDynamicConfig を付与
    dynamic_config_url: str = ""      # ON時の -CustomDynamicConfigUrl=(ASAは起動引数で渡す)

    @property
    def install_root(self) -> Path:
        """このマップのインストールルート(...\\Server1 相当)。SteamCMD更新の単位。"""
        if self.install_dir:
            return Path(self.install_dir)
        return Path(self.exe_path).parents[3] if self.exe_path else Path(".")

    @property
    def gus_path(self) -> Path:
        return Path(self.config_dir) / "GameUserSettings.ini"

    @property
    def game_port(self) -> int | None:
        """launch_argsから ?Port=NNNN(ゲームポート)を取り出す。複数マップの識別に使う。
        RCONPort/Queryport とは区別する(先頭が区切り文字のPortのみ)。"""
        m = re.search(r'[?&"\s]Port=(\d+)', self.launch_args)
        return int(m.group(1)) if m else None

    @property
    def rcon_port_arg(self) -> int | None:
        """launch_argsの RCONPort=NNNN。configを共有しつつマップごとにRCONポートを変える用。"""
        m = re.search(r'RCONPort=(\d+)', self.launch_args)
        return int(m.group(1)) if m else None

    @property
    def query_port(self) -> int | None:
        """launch_argsの Queryport=NNNN(Steamクエリ用UDPポート)。ポート開放に使う。"""
        m = re.search(r'Queryport=(\d+)', self.launch_args, re.IGNORECASE)
        return int(m.group(1)) if m else None

    @property
    def alt_save_dir(self) -> str | None:
        """launch_argsの AltSaveDirectoryName=X(このマップ専用のセーブ先フォルダ名)。"""
        m = re.search(r'AltSaveDirectoryName=([^?&"\s]+)', self.launch_args)
        return m.group(1) if m else None

    @property
    def save_subdir(self) -> str:
        """Saved配下でこのマップのセーブが入るフォルダ名。

        AltSaveDirectoryName未指定(=The Island等のデフォルト)は SavedArks。
        マップごとにバックアップ/復元する対象フォルダを一意に決めるために使う。"""
        return self.alt_save_dir or "SavedArks"

    @property
    def map_label(self) -> str:
        """バックアップのフォルダ/ファイル名に使う、マップの短い識別名。

        例: 'ARK: The Island' -> 'TheIsland'、'ARK: Genesis Part 1' -> 'GenesisPart1'。"""
        name = re.sub(r'^ARK[:：]\s*', '', self.display_name)
        label = re.sub(r'[^A-Za-z0-9]+', '', name)
        return label or self.save_subdir

    @property
    def saved_dir(self) -> Path:
        """...\\ShooterGame\\Saved(config_dir=...\\Saved\\Config\\WindowsServer から求める)。"""
        return Path(self.config_dir).parent.parent

    @property
    def log_file_name(self) -> str:
        """このマップ専用のログファイル名。複数マップが ShooterGame.log を奪い合うのを避ける。"""
        return f"gsm_{self.map_label}.log"

    @property
    def log_path(self) -> Path:
        """このマップのログファイル(Saved/Logs/gsm_<label>.log)。GUIで追尾表示する。"""
        return self.saved_dir / "Logs" / self.log_file_name

    @property
    def effective_launch_args(self) -> str:
        """起動に使う引数。マップ専用ログ(-log=)、dynamic config有効時は
        -CustomDynamicConfigUrl="URL" と -UseDynamicConfig を付与(ASAは起動引数で渡す)。"""
        args = self.launch_args
        if re.search(r'-log=', args, re.IGNORECASE) is None:
            args = f"{args} -log={self.log_file_name}"
        if self.use_dynamic_config and "-usedynamicconfig" not in args.lower():
            if self.dynamic_config_url and "customdynamicconfigurl" not in args.lower():
                args = f'{args} -CustomDynamicConfigUrl="{self.dynamic_config_url}"'
            args = f"{args} -UseDynamicConfig"
        return args


class ArkHostError(Exception):
    pass


def read_ini_value(path: Path, key: str) -> str | None:
    """.ini から key=value の値を1つ取り出す(セクション不問・前方一致)。"""
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith(";") and s.lower().startswith(key.lower() + "="):
            return s.split("=", 1)[1].strip()
    return None


class ArkHost:
    """ホストARKサーバーのファサード。runner=ホストのPowerShell(run_psでCommandResult)。"""

    def __init__(self, cfg: ArkHostConfig, runner):
        self.cfg = cfg
        self.runner = runner

    # ---- 状態 ----
    def uptime_seconds(self) -> int | None:
        """このマップのプロセスの稼働秒数。停止中/取得不可は None。"""
        return uptimes_by_port(self.runner, self.cfg.process_name).get(
            self.cfg.game_port)

    def is_running(self) -> bool:
        """このマップ固有のプロセスが動いているか。複数マップは game_port で識別する。"""
        gp = self.cfg.game_port
        if gp:
            r = self.runner.run_ps(
                f"@(Get-CimInstance Win32_Process -Filter \"Name='{self.cfg.process_name}.exe'\" "
                f"| Where-Object {{ $_.CommandLine -like '*Port={gp}*' }}).Count", timeout=25)
        else:
            r = self.runner.run_ps(
                f"@(Get-Process {self.cfg.process_name} -ErrorAction SilentlyContinue).Count",
                timeout=20)
        try:
            return int((r.stdout or "0").strip() or "0") > 0
        except ValueError:
            return False

    def status(self) -> str:
        return "active" if self.is_running() else "inactive"

    READY_MARKER = "advertising for join"

    def client_version(self) -> str | None:
        """クライアントと同じゲームバージョン(例 '92.4')をログから取得する。

        ビルド番号(24254574等)はプレイヤーには意味が伝わらないため、ログ先頭の
        'ARK Version: X.Y' を拾って表示に使う。ログはマップ専用ログ(gsm_<label>.log)。
        起動時に書かれる行なので、末尾ではなく先頭側から探す。
        """
        p = self.cfg.log_path
        if not p.exists():
            return None
        try:
            with p.open("rb") as f:
                head = f.read(64 * 1024)     # 先頭64KBに起動時の版数行がある
        except OSError:
            return None
        m = re.search(r"ARK Version:\s*([\d.]+)",
                      head.decode("utf-8", "replace"))
        return m.group(1) if m else None

    def is_advertising(self) -> bool:
        """ログに 'advertising for join'(起動完了の合図)が出ているか。

        ASAは「プロセス起動 → 数十秒後に advertising for join」で、advertisingが出て
        初めて実際に参加可能になる。プロセスの有無だけでは『起動中』を『稼働中』と
        誤表示してしまうため、この行で本当の起動完了を判定する。
        注意: ログは起動毎に切り詰められるが長時間稼働で巨大化し、この行は末尾から
        押し出される。よって呼び出し側(監視)は一度Trueになったらラッチし、再確認しない。
        """
        p = self.cfg.log_path
        try:
            size = p.stat().st_size
            with p.open("rb") as f:
                f.seek(max(0, size - 512 * 1024))   # 起動直後はログが小さいので末尾で足りる
                data = f.read()
        except OSError:
            return False
        return self.READY_MARKER in data.decode("utf-8", "replace")

    def rcon_params(self) -> tuple[str, int, str]:
        gus = self.cfg.gus_path
        # RCONポートは launch_args の RCONPort= を優先(マップごとに異なる)、無ければini。
        port = self.cfg.rcon_port_arg or int(read_ini_value(gus, "RCONPort") or "27020")
        pw = read_ini_value(gus, "ServerAdminPassword") or ""
        return self.cfg.rcon_host, int(port), pw

    # ---- RCON ----
    def _rcon(self) -> RconClient:
        host, port, pw = self.rcon_params()
        if not pw:
            raise ArkHostError(
                "RCON管理パスワードが取得できません(GameUserSettings.ini)")
        return RconClient(host, port, pw)

    def rcon_command(self, cmd: str) -> str:
        with self._rcon() as r:
            return r.command(cmd)

    def players(self) -> str:
        try:
            return self.rcon_command("ListPlayers").strip() or "(0人)"
        except (RconError, OSError, ArkHostError) as e:
            return f"RCON接続不可 ({e})"

    def num_players(self) -> int:
        """接続人数。ListPlayersの "N. 名前, ID" 行を数える。取得失敗時は0。"""
        try:
            raw = self.rcon_command("ListPlayers")
        except Exception:
            return 0
        return len(re.findall(r'(?m)^\s*\d+\.\s', raw or ""))

    # ---- ゲーム内アナウンス(チャット) ----
    # ※ このASAはRCON経由の日本語チャットを描画できない(UTF-8/CP932とも文字化け)ため、
    #   予告文は英数字(ASCII)で送る。Broadcast(中央表示)もこの個体では描画されないためChatを使う。
    def announce(self, msg: str) -> None:
        """全員のチャット欄へメッセージを送る(失敗しても本処理は止めない)。"""
        try:
            self.rcon_command("ServerChat " + msg)
        except Exception:
            pass

    def announce_countdown(self, schedule, progress=lambda t: None) -> None:
        """schedule=[(残り秒, 文面), ...] を残り秒の降順で告知しつつ待つ。

        例: [(60,..),(30,..),(10,..)] なら 告知→30秒待ち→告知→20秒待ち→告知→10秒待ち。"""
        items = sorted(schedule, key=lambda x: -x[0])
        for i, (secs, msg) in enumerate(items):
            progress(f"予告(残り{secs}秒): {msg}")
            self.announce(msg)
            next_secs = items[i + 1][0] if i + 1 < len(items) else 0
            wait = max(0, secs - next_secs)
            if wait:
                time.sleep(wait)

    def info(self) -> dict:
        return {
            "session": read_ini_value(self.cfg.gus_path, "SessionName") or "-",
            "running": self.is_running(),
            "players": self.players() if self.is_running() else "(停止中)",
        }

    # ---- 起動 / 停止 ----
    def start(self, progress=lambda t: None) -> None:
        if self.is_running():
            raise ArkHostError("既にARKサーバーが起動しています(二重起動防止)")
        if not Path(self.cfg.exe_path).exists():
            raise ArkHostError(f"exeが見つかりません: {self.cfg.exe_path}")
        wd = str(Path(self.cfg.exe_path).parent)
        progress("ARKサーバーを起動中…(コンソールは非表示。ログはGUIで表示)")
        # -WindowStyle Hidden: 別コンソールウィンドウを出さない(ユーザー要望)。
        # -log=gsm_<label>.log: マップ専用ログへ出力 → GUIがこのファイルを追尾表示する。
        r = self.runner.run_ps(
            f"Start-Process -FilePath '{self.cfg.exe_path}' "
            f"-WorkingDirectory '{wd}' -WindowStyle Hidden "
            f"-ArgumentList '{self.cfg.effective_launch_args}'; 'STARTED'",
            timeout=60)
        if "STARTED" not in (r.stdout or ""):
            raise ArkHostError(f"起動に失敗しました: {r.stderr.strip()}")

    def tail_log(self, lines: int = 400) -> str:
        """このマップのログファイル末尾を返す(GUIのコンソール表示用)。

        サーバーが書き込み中でも共有読み取りで開ける(UEは共有読み取りでログを開く)。"""
        p = self.cfg.log_path
        if not p.exists():
            return ("(ログファイルがまだありません)\n"
                    "GSMから起動するとマップ専用ログが生成され、ここに表示されます。\n"
                    f"想定パス: {p}")
        try:
            data = p.read_bytes()
        except OSError as e:
            return f"(ログを読み取れません: {e})"
        text = data.decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])

    def tail_log_since(self, offset: int = 0, lines: int = 400) -> tuple[str, int]:
        """(増えた分のテキスト, 新しいオフセット) を返す。ライブ表示用。

        毎回ファイル全体(=数MB相当のJSON)を返すと通信も描画も無駄なので、
        前回読んだバイト位置以降だけを読む。offset=0 なら末尾 lines 行を返す
        (初回表示用)。ログが縮んだ(ローテートされた)場合は先頭から読み直す。
        """
        p = self.cfg.log_path
        if not p.exists():
            return ("(ログファイルがまだありません)\n"
                    f"想定パス: {p}", 0)
        try:
            size = p.stat().st_size
            if offset and offset <= size:
                with p.open("rb") as f:      # 前回位置から増分だけ読む
                    f.seek(offset)
                    data = f.read()
                return data.decode("utf-8", "replace"), size
            # 初回 or ローテート検知 → 末尾lines行
            with p.open("rb") as f:
                f.seek(max(0, size - 512 * 1024))   # 末尾512KBだけ見れば十分
                data = f.read()
            text = data.decode("utf-8", "replace")
            return "\n".join(text.splitlines()[-lines:]), size
        except OSError as e:
            return f"(ログを読み取れません: {e})", 0

    def stop(self, progress=lambda t: None) -> None:
        if not self.is_running():
            return
        try:
            progress("ワールド保存中(saveworld)…")
            self.rcon_command("saveworld")
            time.sleep(2)
            progress("サーバー終了指示(DoExit)…")
            self.rcon_command("DoExit")
        except Exception:
            pass  # RCON不通でも下でプロセス停止する
        for _ in range(20):                 # 最大60秒、消滅を待つ
            if not self.is_running():
                progress("停止しました(保存済み)")
                return
            time.sleep(3)
        progress("応答が無いためプロセスを停止…")
        gp = self.cfg.game_port
        if gp:                              # このマップのプロセスだけを止める
            self.runner.run_ps(
                f"Get-CimInstance Win32_Process -Filter \"Name='{self.cfg.process_name}.exe'\" "
                f"| Where-Object {{ $_.CommandLine -like '*Port={gp}*' }} "
                f"| ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}", timeout=30)
        else:
            self.runner.run_ps(
                f"Get-Process {self.cfg.process_name} -ErrorAction SilentlyContinue "
                f"| Stop-Process -Force", timeout=30)

    def restart(self, progress=lambda t: None) -> None:
        self.stop(progress=progress)
        time.sleep(3)
        self.start(progress=progress)

    # ---- 野生恐竜リスポーン ----
    def destroy_wild_dinos(self) -> str:
        """野生恐竜を全消去(自然リスポーンされる)。DestroyWildDinos。"""
        return self.rcon_command("DestroyWildDinos")

    def respawn_wild_dinos_now(self, progress=lambda t: None) -> str:
        """今すぐ野生恐竜をリスポーンし、ゲーム内へ告知する(手動リスポーン用)。"""
        self.announce("[GSM] Respawning wild dinosaurs...")
        resp = self.destroy_wild_dinos()
        self.announce("[GSM] Wild dinosaurs have respawned.")
        progress(f"野生恐竜をリスポーンしました ({resp})")
        return resp

    def wait_ready(self, timeout: int = 360, progress=lambda t: None) -> bool:
        """起動完了(RCONが応答する)まで待つ。落ちたら/超過でFalse。

        ログの 'advertising for join' は前セッションの残骸を誤検出しうるので、
        RCON(ListPlayers)が実際に応答することを起動完了の合図にする。"""
        deadline = time.time() + timeout
        time.sleep(8)                          # 起動直後の猶予(旧プロセス消滅待ち含む)
        while time.time() < deadline:
            if not self.is_running():
                return False
            try:
                self.rcon_command("ListPlayers")   # 応答すればRCON稼働=起動完了
                return True
            except Exception:
                time.sleep(6)
        return False

    def respawn_wild_dinos_after_ready(self, progress=lambda t: None) -> None:
        """起動完了(RCON応答)を待ってから野生恐竜をリスポーンする(再起動直後用)。"""
        progress("起動完了(RCON応答)を待っています…")
        if not self.wait_ready(progress=progress):
            progress("起動確認できず、野生恐竜リスポーンをスキップ")
            return
        time.sleep(2)
        for attempt in range(2):               # 念のため1回リトライ
            try:
                self.destroy_wild_dinos()
                self.announce("[GSM] Wild dinos have respawned.")
                progress("野生恐竜をリスポーンしました(DestroyWildDinos)")
                return
            except Exception as e:
                if attempt == 0:
                    time.sleep(5)
                    continue
                progress(f"DestroyWildDinos に失敗: {e}")

    # ---- 予告付き 再起動 / 停止 ----
    def restart_with_notice(self, notify: bool = True, respawn_dinos: bool = False,
                            progress=lambda t: None) -> None:
        """プレイヤーが居れば60/30/10秒前にチャット予告してから再起動する。
        respawn_dinos=True なら起動完了後に野生恐竜をリスポーンする。"""
        if notify and self.is_running() and self.num_players() > 0:
            self.announce_countdown(notice_schedule("restart"), progress=progress)
            self.announce("[GSM] Restarting now. Back in ~2 minutes.")
        elif self.is_running():
            progress("プレイヤー不在のため予告を省略して再起動します")
        self.restart(progress=progress)
        if respawn_dinos:
            self.respawn_wild_dinos_after_ready(progress=progress)

    def stop_with_notice(self, notify: bool = True, reason: str = "",
                         progress=lambda t: None) -> None:
        """プレイヤーが居れば60/30/10秒前にチャット予告してから停止する。

        reason を指定すると予告文に理由を添える(例: "for a server update")。"""
        if notify and self.is_running() and self.num_players() > 0:
            self.announce_countdown(notice_schedule("shut down", reason), progress=progress)
            tail = f" {reason}" if reason else ""
            self.announce(f"[GSM] Shutting down now{tail}. Thanks for playing!")
        elif self.is_running():
            progress("プレイヤー不在のため予告を省略して停止します")
        self.stop(progress=progress)


def notice_schedule(action_word: str, reason: str = "") -> list[tuple[int, str]]:
    """再起動/停止の予告文(英数字。このASAは日本語チャットを描画できないため)。

    reason を指定すると各予告の末尾に理由を添える。"""
    cap = action_word.capitalize()
    tail = f" {reason}" if reason else ""
    return [
        (60, f"[GSM] Server will {action_word.upper()} in 60 seconds{tail}. "
             "Please find a safe spot and log off."),
        (30, f"[GSM] {cap} in 30 seconds...{tail}"),
        (10, f"[GSM] {cap} in 10 seconds!"),
    ]


def uptimes_by_port(runner, process_name: str = "ArkAscendedServer") -> dict[int, int]:
    r"""全ARKプロセスの (ゲームポート -> 稼働秒数)。

    マップごとに問い合わせると重いので、PowerShell1回で全プロセスの
    「稼働秒数<TAB>コマンドライン」を取り、ポート判定はPython側の正規表現で行う
    (RCONPort/Queryport と区別するため is_running と同じ [?&"\s]Port= を使う)。
    """
    ps = ('Get-CimInstance Win32_Process -Filter "Name=\'' + process_name + '.exe\'" '
          '| ForEach-Object { [string][int](((Get-Date) - $_.CreationDate).TotalSeconds) '
          '+ "`t" + $_.CommandLine }')
    r = runner.run_ps(ps, timeout=25)
    out: dict[int, int] = {}
    for line in (r.stdout or "").splitlines():
        secs, _, cmd = line.partition("\t")
        m = re.search(r'[?&"\s]Port=(\d+)', cmd)
        if not m:
            continue
        try:
            out[int(m.group(1))] = int(secs.strip())
        except ValueError:
            pass
    return out


def format_uptime(seconds: int | None) -> str:
    """稼働秒数を「3日4時間12分」形式に。停止中/不明は ―。"""
    if seconds is None or seconds < 0:
        return "―"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}日{h}時間{m}分"
    if h:
        return f"{h}時間{m}分"
    return f"{m}分"
