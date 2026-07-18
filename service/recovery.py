"""状態変化のハンドリング: Discord通知 + クラッシュ自動復旧。

旧GUIの _server_state_changed / _crash_autorestart 相当を常駐側へ。

「意図的な停止」と「クラッシュ」の区別:
  GSM自身が止めた/再起動した時は mark_stop()/mark_restart() で印を付ける。
  印のない停止 = クラッシュとみなして自動復旧する。印があれば復旧しない
  (この区別が無いと、こちらが止めたサーバーを勝手に起動し直してしまう)。
"""
from __future__ import annotations

import json
import time

from core.orchestration import start_server_with_vm
from core.paths import app_dir

CRASHWATCH_PATH = app_dir() / "crashwatch.json"
MARK_WINDOW_SEC = 300        # 意図的操作とみなす時間窓
COOLDOWN_SEC = 120           # 復旧の連打防止


class RecoveryService:
    def __init__(self, ctx, notifier=None, portsync=None):
        self.ctx = ctx
        self.notifier = notifier
        self.portsync = portsync
        self.enabled = self._load_enabled()
        self._stop_marks: dict[str, float] = {}
        self._restart_marks: dict[str, float] = {}
        self._last_recover: dict[str, float] = {}

    @staticmethod
    def _load_enabled() -> bool:
        try:
            return bool(json.loads(
                CRASHWATCH_PATH.read_text(encoding="utf-8")).get("enabled", False))
        except (OSError, ValueError):
            return False

    def set_enabled(self, enabled: bool) -> dict:
        self.enabled = bool(enabled)
        try:
            CRASHWATCH_PATH.write_text(
                json.dumps({"enabled": self.enabled}), encoding="utf-8")
        except OSError as exc:
            print("crashwatch.json 保存失敗:", exc)
        return {"enabled": self.enabled}

    # ---- GSM自身の操作に印を付ける(クラッシュと区別するため) ----
    def mark_stop(self, key: str) -> None:
        self._stop_marks[key] = time.time()

    def mark_restart(self, key: str) -> None:
        self._restart_marks[key] = time.time()

    def _recent(self, marks: dict, key: str) -> bool:
        t = marks.get(key)
        return bool(t and time.time() - t < MARK_WINDOW_SEC)

    # ---- Monitor から呼ばれる ----
    def on_ready(self, key: str, display: str, game: str | None = None) -> None:
        """本当の起動完了(ARKは advertising for join、MC/Palは稼働)で呼ばれる。

        『起動しました』通知はプロセス起動時ではなくここで出す。ARKはプロセスが
        立ち上がってから実際に参加可能になるまで数十秒あり、早すぎる通知は誤解を招くため。
        """
        if not self._recent(self._restart_marks, key):
            self._notify("server_up", f"🟢 {display} が起動しました", game)

    def on_change(self, key: str, kind: str, display: str, running: bool, ref,
                  game: str | None = None) -> None:
        if running:
            pass                           # 起動通知は on_ready(起動完了)で出す
        else:
            if self._recent(self._restart_marks, key):
                pass                       # 再起動途中の一時停止 → 無音
            elif self._recent(self._stop_marks, key):
                self._notify("server_down", f"⚪ {display} を停止しました", game)
            else:
                self._notify("crash", f"⚠ {display} が予期せず停止しました(クラッシュ?)",
                             game)
                if self.enabled:
                    self._recover(key, kind, display, ref, game)
        if self.portsync:                  # 起動/停止に合わせてポートを即開閉
            self.portsync.on_state_change()

    def _recover(self, key: str, kind: str, display: str, ref,
                 game: str | None = None) -> None:
        last = self._last_recover.get(key, 0)
        if time.time() - last < COOLDOWN_SEC:
            print(f"自動復旧クールダウン中のためスキップ: {display}")
            return
        self._last_recover[key] = time.time()
        jobs = self.ctx.jobs

        def fn():
            if kind == "ark":
                if not ref.is_running():
                    jobs.progress(f"{display}: 起動…")
                    ref.start(progress=jobs.progress)
                    ref.wait_ready(progress=jobs.progress)
            else:
                jobs.progress(f"{display}: VM込みで起動…")
                start_server_with_vm(self.ctx.hyperv, ref, progress=jobs.progress)
            return "recovered"

        def done(_r, error):
            if error is None:
                self._notify("crash", f"✅ {display} を自動復旧しました", game)
            else:
                self._notify("crash", f"❌ {display} の自動復旧に失敗: {error}", game)
        lane = key.replace(":", "-")
        jobs.submit(f"🔧 自動復旧: {display}", fn, lane=lane, category="自動復旧",
                    on_done=done)

    def _notify(self, event: str, text: str, game: str | None = None) -> None:
        if self.notifier:
            try:
                self.notifier(event, text, game)
            except Exception:
                pass
