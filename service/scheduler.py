"""予約(バックアップ/更新/再起動)の常駐スケジューラ。

旧GUI実装からの改善点(いずれも2026-07-17に実際に事故った箇所):
  1. tkinterの after() ではなく専用スレッドで回す → GUIが無くても動く(今回の主目的)。
  2. 「バックアップ→更新→再起動」をコールバック連鎖ではなく1ジョブ内で順に実行する。
     旧実装は更新に6時間かかった結果、後段の再起動が11:01に発火した(朝5時の予約なのに)。
     1ジョブ内なら途中で経過時間を見て打ち切れる。
  3. 間隔モードの起点をファイルに永続化 → GUI/サービス再起動でリセットされない。
     旧実装はメモリ保持だったため、再起動のたびに10分待ちが振り出しに戻っていた。
  4. マップごとにレーンを分けるので、更新中でも他マップやプレイヤーBKは動く。
"""
from __future__ import annotations

import json
import threading
import datetime as _dt
from pathlib import Path

from core import arkupdate, backup, scheduler as sched_core
from core.paths import app_dir

from .runner import PLAYERS_LANE, ark_lane, server_lane

SCHEDULES_PATH = app_dir() / "schedules.json"
SCHED_STATE_PATH = app_dir() / "sched_state.json"   # 間隔モードの最終発火時刻
TICK_SEC = 20
# 前段(更新など)が長引いた時に再起動を諦める上限。予約時刻から離れすぎた再起動は事故。
CHAIN_MAX_DELAY_SEC = 3600


class SchedulerService:
    def __init__(self, ctx, state=None, notifier=None, recovery=None):
        self.ctx = ctx
        self.state = state
        self.notifier = notifier
        self.recovery = recovery      # 予約の停止/再起動をクラッシュと誤検知させないため
        self.jobs = ctx.jobs
        self.schedules = sched_core.load_jobs(SCHEDULES_PATH)
        self._fired: set = set()               # (id, HH:MM, YYYY-MM-DD) 二重発火抑止
        self._interval_last: dict = self._load_interval_state()
        self._inflight: set = set()            # 実行中の間隔ジョブID
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- 常駐部品 ----
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="gsm-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(_dt.datetime.now())
            except Exception as exc:
                print("スケジューラtickで例外:", exc)
            self._stop.wait(TICK_SEC)

    # ---- 間隔モードの起点を永続化(GUI再起動でリセットされないように) ----
    def _load_interval_state(self) -> dict:
        try:
            return json.loads(Path(SCHED_STATE_PATH).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_interval_state(self) -> None:
        try:
            Path(SCHED_STATE_PATH).write_text(
                json.dumps(self._interval_last, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            print("sched_state.json 保存失敗:", exc)

    # ---- 発火判定 ----
    def tick(self, now: _dt.datetime) -> None:
        for job in sched_core.due_jobs(self.schedules, now):
            key = (job.id, now.strftime("%H:%M"), now.strftime("%Y-%m-%d"))
            if key in self._fired:
                continue
            self._fired.add(key)
            self.fire(job)
        today = now.strftime("%Y-%m-%d")
        self._fired = {k for k in self._fired if k[2] == today}
        self._interval_tick(now)

    def _interval_tick(self, now: _dt.datetime) -> None:
        changed = False
        for job in self.schedules:
            if not job.enabled or not job.is_interval():
                continue
            last = self._interval_last.get(job.id)
            if last is None:
                self._interval_last[job.id] = now.timestamp()
                changed = True
                continue
            if now.timestamp() - last >= job.interval_min * 60:
                if job.id in self._inflight:      # 前回がまだ走っていれば見送る
                    continue
                self._interval_last[job.id] = now.timestamp()
                changed = True
                self.fire(job)
        if changed:
            self._save_interval_state()

    # ---- 実行 ----
    def fire(self, job) -> None:
        if job.kind == "ark-players":
            self._fire_players(job)
        elif job.kind == "ark-all":
            self._fire_ark_all(job)
        elif job.kind == "ark":
            ah = self.ctx.ark_by_label(job.target)
            if ah is None:
                print(f"予約: ARK '{job.target}' が見つからずスキップ")
                return
            self._submit_ark(job, ah)
        else:
            self._fire_server(job)

    def fire_by_id(self, sid: str) -> None:
        for j in self.schedules:
            if j.id == sid:
                self.fire(j)
                return
        raise KeyError(sid)

    # ---- ARK: 1マップぶんの「BK→更新→再起動」を1ジョブで順に実行 ----
    def _submit_ark(self, job, ah) -> None:
        label = ah.cfg.map_label
        name = ah.cfg.display_name

        def fn():
            t0 = _dt.datetime.now()
            out = []
            if job.do_backup:
                out.append(self._do_backup(ah))
            if job.do_update:
                out.append(self._do_update(ah))
            if job.do_restart:
                late = (_dt.datetime.now() - t0).total_seconds()
                if late > CHAIN_MAX_DELAY_SEC:
                    # 朝5時の予約の再起動が昼に飛ぶ事故を防ぐ(2026-07-17に実際に発生)
                    msg = (f"前段に{late / 60:.0f}分かかったため再起動を中止"
                           f"(予約時刻から{CHAIN_MAX_DELAY_SEC // 60}分以上ズレるため)")
                    self.jobs.progress("⏭ " + msg)
                    self._notify("restart", f"⏰ {name}: {msg}", "ark")
                    out.append("再起動中止(遅延)")
                else:
                    out.append(self._do_restart(ah))
            return " / ".join(out) or "(なし)"

        self.jobs.submit(f"⏰ 予約({job.action_text()}): {name}", fn,
                         lane=ark_lane(label), category="予約")

    def _do_backup(self, ah) -> str:
        self.jobs.progress(f"{ah.cfg.display_name}: バックアップ中…")
        try:
            p = backup.ark_backup(
                str(backup.ark_saved_dir(ah.cfg.config_dir)), self.ctx.backupcfg,
                ah.cfg.map_label, ah.cfg.save_subdir, progress=self.jobs.progress)
            return f"BK={Path(p).name}"
        except backup.BackupError as exc:
            # 未起動マップはセーブが無い。ここで止めると後続(本番マップ)が巻き添えになる
            self.jobs.progress(f"セーブ無しのためBKスキップ({exc})")
            return "BKスキップ"

    def _mark(self, ah, kind: str = "restart") -> None:
        """GSM自身の停止/再起動に印を付ける(クラッシュ自動復旧の誤発火を防ぐ)。"""
        if not self.recovery:
            return
        try:
            key = f"ark:{self.ctx.arkhosts.index(ah)}"
        except ValueError:
            return
        (self.recovery.mark_restart if kind == "restart"
         else self.recovery.mark_stop)(key)

    def _do_update(self, ah) -> str:
        if not self.ctx.ark_steamcmd:
            return "更新スキップ(steamcmd無し)"
        latest = arkupdate.latest_buildid(self.ctx.ark_steamcmd)
        cur = arkupdate.installed_buildid(ah.cfg.install_root)
        if latest and cur == latest:
            self.jobs.progress(f"{ah.cfg.display_name}: 最新({cur})")
            return "最新"
        was = ah.is_running()
        if was:
            self._mark(ah, "restart")      # 更新中の停止=意図的(復旧させない)
            self.jobs.progress("更新のため停止(予告あり)…")
            ah.stop_with_notice(progress=self.jobs.progress)
        try:
            new = arkupdate.update(self.ctx.ark_steamcmd, ah.cfg.install_root,
                                   progress=self.jobs.progress)
            return f"更新 {cur}→{new}"
        finally:
            if was:                       # 失敗しても必ず戻す
                self.jobs.progress("起動…")
                ah.start(progress=self.jobs.progress)
                ah.wait_ready(progress=self.jobs.progress)

    def _do_restart(self, ah) -> str:
        if not ah.is_running():
            self.jobs.progress(f"{ah.cfg.display_name}: 停止中のため再起動スキップ")
            return "停止中スキップ"
        self._mark(ah, "restart")          # 予約再起動=意図的(復旧させない)
        respawn = self._respawn_flag()
        # cancelable=True: プレイヤーがチャットに 'no' を送ると予約再起動を中止できる
        if not ah.restart_with_notice(respawn_dinos=respawn, cancelable=True,
                                      progress=self.jobs.progress):
            return "プレイヤーがチャットで中止"
        ah.wait_ready(progress=self.jobs.progress)
        return "再起動"

    @staticmethod
    def _respawn_flag() -> bool:
        try:
            p = app_dir() / "arkbehavior.json"
            return bool(json.loads(p.read_text(encoding="utf-8")).get(
                "respawn_on_restart", False))
        except (OSError, ValueError):
            return False

    def _fire_ark_all(self, job) -> None:
        if job.rolling:
            order = job.order or [a.cfg.map_label for a in self.ctx.arkhosts]
            seq = [a for lbl in order for a in self.ctx.arkhosts
                   if a.cfg.map_label == lbl]
            seq += [a for a in self.ctx.arkhosts if a not in seq]

            def fn():
                t0 = _dt.datetime.now()
                for ah in seq:
                    if job.do_backup:
                        self._do_backup(ah)
                    if job.do_update:
                        self._do_update(ah)
                    if job.do_restart:
                        late = (_dt.datetime.now() - t0).total_seconds()
                        if late > CHAIN_MAX_DELAY_SEC:
                            self.jobs.progress("⏭ 遅延のため以降の再起動を中止")
                            break
                        self._do_restart(ah)
                return "done"
            self.jobs.submit(f"⏰ ローリング({job.action_text()}): ARK全マップ", fn,
                             lane="ark-rolling", category="予約")
            return
        # 非ローリング: マップごとに別レーン = 並列に走る(1台の長い更新が他を止めない)
        for ah in self.ctx.arkhosts:
            self._submit_ark(job, ah)

    # ---- プレイヤーデータのみ(軽量・saveworldしない) ----
    def _fire_players(self, job) -> None:
        entries = [(a.cfg.map_label, str(backup.ark_saved_dir(a.cfg.config_dir)),
                    a.cfg.save_subdir) for a in self.ctx.arkhosts]
        cluster = self.ctx.ark_cluster_dir()
        keep = job.keep or None
        self._inflight.add(job.id)

        def fn():
            return Path(backup.ark_player_backup(
                entries, cluster, self.ctx.backupcfg, keep=keep,
                progress=self.jobs.progress)).name

        def done(_r, _e):
            self._inflight.discard(job.id)
        self.jobs.submit(f"🧬 プレイヤーデータBK({job.interval_min}分毎)", fn,
                         lane=PLAYERS_LANE, category="定期バックアップ", on_done=done)

    # ---- MC / Palworld ----
    def _fire_server(self, job) -> None:
        srv = self.ctx.servers.get(job.target)
        if srv is None:
            print(f"予約: サーバー '{job.target}' が見つからずスキップ")
            return

        def fn():
            out = []
            if job.do_backup:
                p = (backup.pal_backup if srv.profile.game == "palworld"
                     else backup.mc_backup)(srv.profile, self.ctx.backupcfg,
                                            progress=self.jobs.progress)
                out.append(f"BK={Path(p).name}")
            if job.do_restart:
                if srv.status() != "active":
                    self.jobs.progress("停止中のため再起動スキップ")
                    out.append("停止中スキップ")
                else:
                    # 予約再起動=意図的。マークしてクラッシュ誤検知を防ぐ(完了で🔁通知)
                    if self.recovery:
                        self.recovery.mark_restart(f"mc:{job.target}")
                    # cancelable=True: Minecraftはチャット 'no' で中止可(Palworldは読取不可)
                    ok = srv.restart_with_notice(progress=self.jobs.progress,
                                                 cancelable=True)
                    out.append("再起動" if ok else "プレイヤーがチャットで中止")
            return " / ".join(out) or "(なし)"
        self.jobs.submit(f"⏰ 予約({job.action_text()}): {srv.profile.display_name}",
                         fn, lane=server_lane(job.target), category="予約")

    # ---- API向け ----
    def as_dicts(self) -> list[dict]:
        from dataclasses import asdict
        out = []
        for j in self.schedules:
            d = asdict(j)
            d["action_text"] = j.action_text()
            d["times_text"] = j.times_text()
            d["days_text"] = j.days_text()
            d["next_interval_in_sec"] = self._next_in(j)
            out.append(d)
        return out

    def _next_in(self, job) -> float | None:
        if not job.is_interval():
            return None
        last = self._interval_last.get(job.id)
        if last is None:
            return job.interval_min * 60
        return max(0.0, last + job.interval_min * 60 - _dt.datetime.now().timestamp())

    def replace_all(self, dicts: list[dict]) -> None:
        with self._lock:
            jobs = []
            for d in dicts:
                jobs.append(sched_core.RestartJob(
                    id=str(d["id"]), kind=d.get("kind", "mc"),
                    target=d.get("target", ""), display=d.get("display", ""),
                    times=list(d.get("times", [])),
                    days=[int(x) for x in d.get("days", [])],
                    enabled=bool(d.get("enabled", True)),
                    do_backup=bool(d.get("do_backup", False)),
                    do_update=bool(d.get("do_update", False)),
                    do_restart=bool(d.get("do_restart", True)),
                    rolling=bool(d.get("rolling", False)),
                    order=list(d.get("order", [])),
                    interval_min=int(d.get("interval_min", 0) or 0),
                    keep=int(d.get("keep", 0) or 0)))
            self.schedules = jobs
            sched_core.save_jobs(SCHEDULES_PATH, jobs)

    def _notify(self, event: str, text: str, game: str | None = None) -> None:
        if self.notifier:
            try:
                self.notifier(event, text, game)
            except Exception:
                pass
