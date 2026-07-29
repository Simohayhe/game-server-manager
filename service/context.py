"""サービス全体で共有するコンテキスト(設定・サーバー実体・ジョブキュー)。

GUIの App.__init__ が抱えていた「config読み込み → ArkHost/GameServer を組み立てる」部分を
UI非依存の形でここに集約する。GUI・API・スケジューラは全部ここを参照する。
"""
from __future__ import annotations

import threading
from pathlib import Path

from core import backup as backup_mod
from core.arkhost import ArkHost
from core.config import load_config
from core.paths import app_dir
from core.transport import LocalPowerShell

from .runner import JobQueue

CONFIG_PATH = app_dir() / "config.yaml"


class Context:
    """設定と各サーバーの実体を保持する。reload() で config.yaml を読み直せる。"""

    def __init__(self, config_path: str | Path = CONFIG_PATH):
        self.config_path = Path(config_path)
        self._lock = threading.Lock()
        # タスク履歴を tasks.json に永続化(サービス/GUIを再起動しても残る)
        self.jobs = JobQueue(persist_path=app_dir() / "tasks.json")
        self.runner = LocalPowerShell()
        self.reload()

    def reload(self) -> None:
        """config.yaml を読み直して各サーバー実体を作り直す。"""
        with self._lock:
            cfg = load_config(self.config_path)
            self.config = cfg
            self.backupcfg: backup_mod.BackupConfig = cfg.backup
            self.ark_steamcmd = getattr(cfg, "ark_steamcmd", "") or ""
            self.direct = (getattr(cfg, "deployment", "hyperv") == "direct")
            self.arkhosts = [ArkHost(c, self.runner) for c in cfg.ark_hosts]
            self._apply_ark_event()
            from core.hyperv import HyperVManager
            self.hyperv = HyperVManager(self.runner)
            if self.direct:
                # 直接モード: VM/SSHなし。このPC上のプロセスとして動かす。
                from core.localserver import LocalGameServer
                self.servers = {p.name: LocalGameServer(p) for p in cfg.servers}
            else:
                # 通常(Hyper-V)モード: GameServer が中でSSHを張る。
                from core.gameserver import GameServer  # 遅延import(循環回避)
                self.servers = {p.name: GameServer(p) for p in cfg.servers}

    # ---- ARK季節イベント(-ActiveEvent) ----
    def ark_event_path(self) -> Path:
        return app_dir() / "arkevent.json"

    def ark_event(self) -> str:
        """保存中の季節イベント名(なし="")。"""
        import json
        try:
            return str(json.loads(
                self.ark_event_path().read_text(encoding="utf-8")).get("event", ""))
        except (OSError, ValueError):
            return ""

    def _apply_ark_event(self) -> None:
        """保存中のイベントを全マップの起動引数フラグに反映(次回起動から効く)。"""
        ev = self.ark_event()
        for a in self.arkhosts:
            a.cfg.active_event = ev

    def set_ark_event(self, event: str) -> str:
        """イベントを保存し、全マップへ即反映する(再起動は呼び出し側の判断)。"""
        import json
        ev = (event or "").strip()
        self.ark_event_path().write_text(
            json.dumps({"event": ev}), encoding="utf-8")
        for a in self.arkhosts:
            a.cfg.active_event = ev
        return ev

    # ---- 参照ヘルパ ----
    def ark_by_label(self, map_label: str) -> ArkHost | None:
        for a in self.arkhosts:
            if a.cfg.map_label == map_label:
                return a
        return None

    def ark_by_index(self, idx: int) -> ArkHost | None:
        return self.arkhosts[idx] if 0 <= idx < len(self.arkhosts) else None

    def ark_cluster_dir(self) -> str | None:
        """-ClusterDirOverride="..." からクラスタ共有フォルダを取り出す。"""
        import re
        for a in self.arkhosts:
            m = re.search(r'-ClusterDirOverride="?([^"\s]+)"?', a.cfg.launch_args)
            if m:
                return m.group(1)
        return None
