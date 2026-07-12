"""ゲームサーバーのプロファイルと操作。

ゲーム固有の知識は持たず、config.yamlのプロファイル定義
(systemdサービス名・RCON設定・コマンド上書き)だけで動く。
新しいゲームはプロファイルを1ブロック追加すれば対応できる。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .rcon import RconClient, RconError
from .transport import SSHTransport


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
            return f"RCON接続不可 ({exc})"

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
