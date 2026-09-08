"""GSM本体(GitHubリリース)の更新チェック。

常駐サービスが定期的にGitHubを見て、新バージョンが出ていたら:
  - 状態をキャッシュして API(/api/appupdate)で GUI/Web/CLI に見せる
  - Discordへ1回だけ通知する(同じバージョンで繰り返し鳴らさない)

「更新が来たことに気づけない」ことが一番の問題なので、通知は控えめだが確実に
1回出す。更新内容(リリースノート)も一緒に配って、何が変わるか分かるようにする。
"""
from __future__ import annotations

import json
import threading
import time

from core import updatecheck
from core.paths import app_dir

STATE_PATH = app_dir() / "appupdate.json"      # 最後に通知したバージョン
CHECK_INTERVAL_SEC = 6 * 3600                  # 6時間ごと
FIRST_DELAY_SEC = 60                           # 起動直後の負荷を避けて少し待つ


class AppUpdateService:
    def __init__(self, ctx, repo: str, current: str, notifier=None,
                 interval_sec: int = CHECK_INTERVAL_SEC):
        self.ctx = ctx
        self.repo = repo
        self.current = current
        self.notifier = notifier
        self.interval = interval_sec
        self._last: dict = {"current": current, "latest": None,
                            "update_available": False, "error": "not-checked",
                            "notes": "", "assets": [], "checked_at": None}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- 常駐部品 ----
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="gsm-appupdate",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(FIRST_DELAY_SEC):
            return
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as exc:                      # noqa: BLE001
                print("本体更新チェックで例外:", exc)
            if self._stop.wait(self.interval):
                return

    # ---- 状態 ----
    def status(self) -> dict:
        with self._lock:
            return dict(self._last)

    def check(self, notify: bool = True) -> dict:
        """GitHubを見て結果をキャッシュする。新バージョンなら1回だけ通知する。"""
        res = updatecheck.check_latest(self.repo, self.current)
        res["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._last = res
        if notify and res.get("update_available") and res.get("latest"):
            self._notify_once(res)
        return res

    # ---- 通知(同じバージョンでは1回だけ) ----
    def _notify_once(self, res: dict) -> None:
        latest = str(res.get("latest") or "")
        if self._already_notified(latest):
            return
        if self.notifier:
            head = (res.get("notes") or "").strip().splitlines()
            summary = "\n".join(head[:8])                 # 冒頭だけ(長文は貼らない)
            text = (f"🆙 GSMの新バージョン **{latest}** が公開されています"
                    f"(現在 {res.get('current')})\n{res.get('url')}")
            if summary:
                text += f"\n\n{summary}"
            try:
                self.notifier("app_update", text, None)
            except Exception as exc:                      # noqa: BLE001
                print("本体更新の通知に失敗:", exc)
        self._mark_notified(latest)

    @staticmethod
    def _already_notified(version: str) -> bool:
        try:
            d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return str(d.get("notified")) == version
        except (OSError, ValueError):
            return False

    @staticmethod
    def _mark_notified(version: str) -> None:
        try:
            STATE_PATH.write_text(json.dumps({"notified": version}),
                                  encoding="utf-8")
        except OSError as exc:
            print("本体更新の通知状態の保存に失敗:", exc)


def add_routes(router, updater: "AppUpdateService") -> None:
    """/api/appupdate を生やす。routes.py を触らずに済むようここで登録する。"""
    def get(**_):
        return updater.status()

    def post(**_):
        return updater.check()                            # 手動チェック(更新ボタン)
    router.add("GET", "/api/appupdate", get)
    router.add("POST", "/api/appupdate/check", post)
