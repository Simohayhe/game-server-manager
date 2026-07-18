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
            self.arkhosts = [ArkHost(c, self.runner) for c in cfg.ark_hosts]
            # MC/Palworld は GameServer(profile) が中でSSHを張る(gui/app.py:477 と同じ)
            from core.gameserver import GameServer      # 遅延import(循環回避)
            from core.hyperv import HyperVManager
            self.servers = {p.name: GameServer(p) for p in cfg.servers}
            self.hyperv = HyperVManager(self.runner)

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
