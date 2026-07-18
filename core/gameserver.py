"""ゲームサーバーのプロファイルと操作。

ゲーム固有の知識は持たず、config.yamlのプロファイル定義
(systemdサービス名・RCON設定・コマンド上書き)だけで動く。
新しいゲームはプロファイルを1ブロック追加すれば対応できる。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .rcon import RconClient, RconError
from .transport import SSHTransport

RCON_FAIL = "RCON接続不可"        # players() が失敗を示す接頭辞(player_count が判定に使う)

NOTICE_MINUTES = (15, 10, 5, 1)   # 予告カウントダウンのタイミング(残り分・降順)
POLL_SEC = 15                     # カウントダウン中の在席/チャット確認間隔(秒)
CANCEL_WORDS = ("no",)            # チャットでこの単語(単独)を送ると再起動を中止


@dataclass
class RconConfig:
    port: int
    password: str


@dataclass
class GameServerProfile:
    name: str                      # プロファイルキー
    display_name: str
    address: str                   # VMのIPアドレス/ホスト名
    game: str = "minecraft"        # ゲーム種別(一覧のセクション分けに使用: minecraft / ark ...)
    fqdn: str | None = None        # 表示用FQDN(DNS登録済みの公開名)
    ssh_user: str = ""
    vm: str | None = None          # Hyper-V上のVM名(VM連携用)
    ssh_port: int = 22
    ssh_key: str | None = None
    ssh_password: str | None = None
    service: str | None = None     # systemdユニット名
    rcon: RconConfig | None = None
    players_command: str = "list"  # プレイヤー一覧のRCONコマンド(ARKなら ListPlayers)
    game_port: int | None = None   # ゲーム本体の待受ポート(VM内)
    external_port: int | None = None  # 外部公開時のWAN側ポート(SRVで隠蔽)
    install_dir: str = "/opt/minecraft"  # サーバー本体ディレクトリ(mods/の親)
    runtime_user: str = "minecraft"      # サーバー実行ユーザー(mods/の所有者)
    config_file: str = "server.properties"  # 詳細設定で編集するファイル(install_dir相対)
    # 起動ログからバージョンを抜き出す正規表現(グループ1がバージョン)
    version_pattern: str | None = None
    # players応答を解釈する正規表現(グループ: 1=人数, 2=最大人数, 3=名前一覧。2,3は省略可)
    players_pattern: str | None = None
    # serviceから自動生成されるコマンドを個別に上書きできる
    commands: dict[str, str] = field(default_factory=dict)

    @property
    def mods_dir(self) -> str:
        """MODを置くディレクトリ(install_dir/mods)。"""
        return f"{self.install_dir.rstrip('/')}/mods"

    @property
    def config_path(self) -> str:
        """詳細設定で編集する設定ファイルのフルパス。"""
        return f"{self.install_dir.rstrip('/')}/{self.config_file}"

    def command_for(self, action: str) -> str:
        if action in self.commands:
            return self.commands[action]
        if self.service is None:
            raise ValueError(
                f"{self.name}: service名かcommands.{action}のどちらかを設定してください")
        defaults = {
            "start": f"sudo systemctl start {self.service}",
            "stop": f"sudo systemctl stop {self.service}",
            "restart": f"sudo systemctl restart {self.service}",
            "status": f"systemctl is-active {self.service}",
            "log": f"sudo journalctl -u {self.service} -n {{lines}} --no-pager",
        }
        return defaults[action]


class GameServer:
    """1つのゲームサーバーへの操作をまとめたファサード。"""

    def __init__(self, profile: GameServerProfile):
        self.profile = profile
        self._ssh = SSHTransport(
            host=profile.address,
            user=profile.ssh_user,
            port=profile.ssh_port,
            key=profile.ssh_key,
            password=profile.ssh_password,
        )

    def status(self) -> str:
        """'active' / 'inactive' / 'failed' / '接続不可' を返す。"""
        try:
            result = self._ssh.run(self.profile.command_for("status"), timeout=15)
        except Exception:
            return "接続不可"
        text = result.stdout.strip()
        return text if text else "unknown"

    def start(self) -> None:
        self._run_action("start")

    def stop(self) -> None:
        self._run_action("stop")

    def restart(self) -> None:
        self._run_action("restart")

    # ---- ゲーム内メッセージ / 予告付き再起動・停止 ----
    def announce(self, msg: str) -> None:
        """ゲーム内へブロードキャスト。MC=say / Palworld=Broadcast(空白は_に置換)。

        失敗しても本処理は止めない(告知はベストエフォート)。
        """
        if self.profile.rcon is None:
            return
        if self.profile.game == "palworld":
            # PalworldのBroadcastは空白で文が切れる仕様 → アンダースコアに置換
            cmd = "Broadcast " + msg.replace(" ", "_")
        else:
            cmd = "say " + msg
        try:
            self.rcon_command(cmd)
        except Exception:
            pass

    def _save_world(self) -> None:
        """再起動前にワールドを保存(取りこぼし防止)。"""
        if self.profile.rcon is None:
            return
        cmd = "Save" if self.profile.game == "palworld" else "save-all"
        try:
            self.rcon_command(cmd)
        except Exception:
            pass

    def _notice_countdown(self, verb: str, progress, seconds=(60, 30, 10),
                          cancelable: bool = False) -> bool:
        """プレイヤーが居れば seconds の各タイミングで予告する(降順・Minecraft用)。

        cancelable=True で、チャットに 'no' が来たら中止(戻り値 False)。
        Minecraftのチャットはサーバーログから読む。
        """
        try:
            n = self.player_count()
        except Exception:
            n = None
        if not n or n <= 0:
            return True
        if cancelable:
            self._log_chat_watch_init()      # カウントダウン前の発言を無視する基準を作る
        items = sorted(seconds, reverse=True)
        for i, sec in enumerate(items):
            hint = " (type 'no' in chat to cancel)" if cancelable else ""
            progress(f"予告(残り{sec}秒): {verb}")
            self.announce(f"Server {verb} in {sec} seconds{hint}")
            nxt = items[i + 1] if i + 1 < len(items) else 0
            wait = sec - nxt
            if wait > 0:
                res = self._wait_countdown(wait, cancelable, progress)
                if res == "cancel":
                    self.announce(f"Server {verb} cancelled by player")
                    progress(f"プレイヤーがチャットで中止 → {verb}を取り消しました")
                    return False
                if res == "empty":
                    break                    # 無人になった → そのまま実行へ
        return True

    def restart_with_notice(self, progress=lambda t: None,
                            cancelable: bool = False) -> bool:
        """プレイヤーが居れば予告してから再起動する。戻り値: 中止されたら False。"""
        if self.profile.game == "palworld":
            return self._palworld_notice("restart", progress, cancelable)
        if not self._notice_countdown("restart", progress, cancelable=cancelable):
            return False                     # プレイヤーが中止 → 再起動しない
        self._save_world()
        progress("再起動中…")
        self.restart()
        return True

    def stop_with_notice(self, progress=lambda t: None) -> None:
        if self.profile.game == "palworld":
            self._palworld_notice("stop", progress)
            return
        self._notice_countdown("shutdown", progress)     # 停止は中止不可
        self._save_world()
        progress("停止中…")
        self.stop()

    # ---- サーバーログからチャットを読んで中止ワードを検出 ----
    #   Minecraft: `<Player> message` / Palworld: `[CHAT] <Player> message`
    #   どちらもログに `<Player> message` の形で出るので同じ読み方で拾える。
    def _log_chat_watch_init(self) -> None:
        """現在のログ末尾を『既読』として記録し、以降の新規発言だけを見る。"""
        self._chat_seen: set[str] = set()
        try:
            for line in self.tail_log(60).splitlines():
                self._chat_seen.add(line)
        except Exception:
            self._chat_seen = set()

    def _log_chat_has_cancel(self) -> bool:
        """新規のチャット行に中止ワード(単独 no)があれば True。"""
        try:
            lines = self.tail_log(60).splitlines()
        except Exception:
            return False
        seen = getattr(self, "_chat_seen", None)
        if seen is None:
            seen = self._chat_seen = set()
        hit = False
        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            m = re.search(r"<[^>]+>\s+(.*)$", line)   # `<Player> message`
            if m:
                body = re.sub(r"[^a-z]", "", m.group(1).strip().lower())
                if body in CANCEL_WORDS:
                    hit = True
        return hit

    def _chat_cancel_hit(self) -> bool:
        """このサーバーのチャットに中止ワードが来たか。

        Minecraft も Palworld もサーバーログにチャットが出るのでログから読む。
        """
        if self.profile.game in ("minecraft", "palworld"):
            return self._log_chat_has_cancel()
        return False

    # ---- Palworld: Broadcast予告 + 在席/チャット監視 ----
    def _palworld_notice(self, action: str, progress,
                         cancelable: bool = False) -> bool:
        """15→10→5→1分の順に Broadcast で予告し、時間になったら実行。

        カウントダウン中も監視し、誰もいなくなったら即実行、cancelable時に
        チャットへ 'no' が来たら中止する。戻り値: 中止されたら False。
        """
        jp = "再起動" if action == "restart" else "停止"
        # Palworldでプレイヤーにメッセージが見えるのは Broadcast(左上[SYSTEM]チャット)だけ。
        # Shutdown は画面に何も出さない。日本語はRCON送信で化けるため英数字(ASCII)で送る。
        en = "restart" if action == "restart" else "shutdown"
        try:
            n = self.player_count()
        except Exception:
            n = None
        if n == 0:                       # 誰も居ない → 予告不要・即実行
            progress(f"プレイヤー不在のため予告を省略して{jp}します")
            self._palworld_finalize(action, progress)
            return True
        hint = " - type no in chat to cancel" if cancelable else ""
        if cancelable:
            self._log_chat_watch_init()  # カウントダウン前の発言を無視する
        mins = NOTICE_MINUTES            # (15, 10, 5, 1)
        for idx, m in enumerate(mins):
            self._pal_broadcast(f"Server {en} in {m} min - please log off safely{hint}")
            progress(f"予告(残り{m}分): {jp}")
            nxt = mins[idx + 1] if idx + 1 < len(mins) else 0
            gap = (m - nxt) * 60
            if not gap:
                continue
            res = self._wait_countdown(gap, cancelable, progress)
            if res == "cancel":
                self._pal_broadcast(f"Server {en} cancelled by player")
                progress(f"プレイヤーがチャットで中止 → {jp}を取り消しました")
                return False
            if res == "empty":
                progress(f"プレイヤー不在を検知 → 待たずに{jp}します")
                break
        self._pal_broadcast(f"Server {en} now")
        self._palworld_finalize(action, progress)
        return True

    def _pal_broadcast(self, message: str) -> None:
        """Palworldの左上[SYSTEM]チャットへ表示(失敗しても本処理は止めない)。
        空白は文が切れる仕様なのでアンダースコアに置換する。"""
        try:
            self.rcon_command("Broadcast " + message.replace(" ", "_"))
        except Exception:
            pass

    def _wait_countdown(self, seconds: int, cancelable: bool,
                        progress=lambda t: None) -> str:
        """seconds秒待つ。'cancel'=チャット中止 / 'empty'=無人 / ''=通常経過。"""
        waited = 0
        while waited < seconds:
            step = min(POLL_SEC, seconds - waited)
            time.sleep(step)
            waited += step
            if cancelable and self._chat_cancel_hit():
                return "cancel"
            try:
                if self.player_count() == 0:
                    return "empty"
            except Exception:
                pass
        return ""

    def _palworld_finalize(self, action: str, progress) -> None:
        """実際の停止・再起動を確定する(systemdで確実に)。

        Shutdownで既に落ちていてもsystemctlは冪等: restart=起動, stop=停止のまま。
        Saveはベストエフォート(既に落ちていればRCON失敗を握り潰す)。
        """
        self._save_world()
        if action == "restart":
            progress("再起動中…")
            self.restart()
        else:
            progress("停止中…")
            self.stop()

    def _run_action(self, action: str) -> None:
        result = self._ssh.run(self.profile.command_for(action), timeout=120)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"{action}に失敗しました: {detail}")

    def tail_log(self, lines: int = 100) -> str:
        cmd = self.profile.command_for("log").format(lines=lines)
        result = self._ssh.run(cmd, timeout=30)
        return result.stdout if result.ok else f"ログ取得エラー:\n{result.stderr}"

    @property
    def _rcon_strict(self) -> bool:
        # PalworldのRCONは応答IDを返さない(仕様非準拠)ので厳密チェックを外す
        return self.profile.game != "palworld"

    def players(self) -> str:
        """RCONでプレイヤー情報を取得する(生テキストを返す)。"""
        if self.profile.rcon is None:
            return "RCON未設定"
        try:
            with RconClient(self.profile.address,
                            self.profile.rcon.port,
                            self.profile.rcon.password) as rcon:
                return rcon.command(self.profile.players_command,
                                    strict=self._rcon_strict).strip() or "(応答なし)"
        except (RconError, OSError) as exc:
            return f"{RCON_FAIL} ({exc})"

    def player_count(self, raw: str | None = None) -> int | None:
        """プレイヤー数を返す。取れなければ None(=不明。0人と区別する)。

        Minecraft等は players_pattern("There are N of a max of M ...")で解釈。
        Palworld の ShowPlayers はCSVで1行目がヘッダなので 行数-1 が人数。
        """
        if raw is None:
            raw = self.players()
        if raw.startswith(RCON_FAIL) or raw in ("RCON未設定", "(応答なし)"):
            return None
        parsed = self.parse_players(raw)
        if parsed:
            return parsed[0]
        if self.profile.game == "palworld":
            lines = [l for l in raw.splitlines() if l.strip()]
            return max(0, len(lines) - 1)
        return None

    def detect_version(self, lines: int = 2000) -> str | None:
        """起動ログからゲームのバージョンを抽出する(version_pattern未設定ならNone)。"""
        if not self.profile.version_pattern:
            return None
        cmd = self.profile.command_for("log").format(lines=lines)
        result = self._ssh.run(cmd, timeout=30)
        if not result.ok:
            return None
        # 最後のマッチ = 直近の起動時のバージョン
        matches = re.findall(self.profile.version_pattern, result.stdout)
        return matches[-1] if matches else None

    def parse_players(self, raw: str) -> tuple[int, int | None, str] | None:
        """players()の生テキストを(人数, 最大人数, 名前一覧)に解釈する。

        players_pattern未設定・不一致ならNone(呼び出し側は生テキストを使う)。
        """
        if not self.profile.players_pattern:
            return None
        m = re.search(self.profile.players_pattern, raw)
        if not m:
            return None
        groups = m.groups()
        count = int(groups[0])
        max_players = int(groups[1]) if len(groups) > 1 and groups[1] else None
        names = groups[2].strip() if len(groups) > 2 and groups[2] else ""
        return count, max_players, names

    def rcon_command(self, cmd: str) -> str:
        if self.profile.rcon is None:
            raise RuntimeError(f"{self.profile.name}: RCONが設定されていません")
        with RconClient(self.profile.address,
                        self.profile.rcon.port,
                        self.profile.rcon.password) as rcon:
            return rcon.command(cmd, strict=self._rcon_strict)

    def close(self) -> None:
        self._ssh.close()
