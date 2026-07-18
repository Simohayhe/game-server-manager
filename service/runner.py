"""ジョブ実行エンジン(常駐サービスの心臓部)。

旧GUI(gui/app.py)はキュー1本・ワーカー1本・実行中タスクも1個(self._active_task)だったため、
長い処理が他を全部止めた。実例: 2026-07-17にARK更新が6時間かかり、その間ずっと
10分毎のプレイヤーデータバックアップが実行されなかった(04:02のジョブが11:02に実行)。

そこで「レーン」を導入する:
  - レーンごとに専用スレッド+キューを持つ。同じレーンのジョブは直列、別レーンは並列。
  - 例: ark:Ragnarok の更新中でも players レーンのバックアップは走る。
  - 同一マップへの操作(更新とバックアップ)は同じレーンに入れて直列化=競合を防ぐ。
進捗(progress)は threading.local で「今このスレッドが実行中のタスク」に紐付けるので、
並列に走っても混線しない(旧実装の self._active_task 単一フィールド問題の解消)。
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import threading
import traceback
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

MAX_LOG_LINES = 400          # 1タスクが保持する進捗行の上限(メモリ肥大防止)
MAX_TASKS = 300              # 履歴として保持するタスク数の上限
PERSIST_TASKS = 150          # ディスクに残す件数(起動時の読み込みを軽く)


@dataclass
class Task:
    """1操作の記録。GUIはこれをAPI経由で読んで「タスク画面」に出す。"""
    id: str
    title: str
    category: str = "操作"
    lane: str = "default"
    status: str = "pending"          # pending / running / success / failed
    log: list[str] = field(default_factory=list)
    error: str | None = None
    result: str | None = None
    started: str | None = None
    ended: str | None = None
    _t0: float = 0.0
    duration: float = 0.0

    def add(self, text: str) -> None:
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {text}")
        if len(self.log) > MAX_LOG_LINES:       # 古い行から捨てる
            del self.log[: len(self.log) - MAX_LOG_LINES]

    def as_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "category": self.category,
            "lane": self.lane, "status": self.status, "log": list(self.log),
            "error": self.error, "result": self.result,
            "started": self.started, "ended": self.ended,
            "duration": round(self.duration, 1),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        t = cls(id=str(d.get("id")), title=d.get("title", ""),
                category=d.get("category", "操作"), lane=d.get("lane", "default"),
                status=d.get("status", "success"),
                log=list(d.get("log") or []), error=d.get("error"),
                result=d.get("result"), started=d.get("started"),
                ended=d.get("ended"), duration=float(d.get("duration") or 0.0))
        return t


class JobQueue:
    """レーン単位で直列・レーン間は並列に実行するジョブキュー。"""

    def __init__(self, on_task_change=None, persist_path=None):
        self._lanes: dict[str, queue.Queue] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._local = threading.local()          # スレッド毎の「実行中タスク」
        self._tasks: list[Task] = []             # 新しい順
        self._ids = itertools.count(1)
        self._on_task_change = on_task_change    # 変化時の通知(任意)
        self._persist = Path(persist_path) if persist_path else None
        self._load()                             # 前回までのタスク履歴を復元

    # ---- 投入 ----
    def submit(self, title: str, fn, lane: str = "default", category: str = "操作",
               on_done=None) -> Task:
        """fn() をレーンで実行する。on_done(result, error) は完了時に同スレッドで呼ぶ。

        fn の中からは progress() を呼べば、そのタスクのログに残る。
        """
        task = Task(id=str(next(self._ids)), title=title, category=category, lane=lane)
        task.add("受付(キュー待ち)")
        with self._lock:
            self._tasks.insert(0, task)
            del self._tasks[MAX_TASKS:]
            q = self._ensure_lane(lane)
        self._changed(task)
        self._save()
        q.put((task, fn, on_done))
        return task

    def _ensure_lane(self, lane: str) -> queue.Queue:
        """レーンのキューとワーカースレッドを(無ければ)作る。呼び出し側で _lock 済み。"""
        q = self._lanes.get(lane)
        if q is None:
            q = queue.Queue()
            self._lanes[lane] = q
            t = threading.Thread(target=self._worker, args=(lane, q),
                                 name=f"gsm-lane-{lane}", daemon=True)
            self._threads[lane] = t
            t.start()
        return q

    # ---- 実行 ----
    def _worker(self, lane: str, q: queue.Queue) -> None:
        while True:
            task, fn, on_done = q.get()
            self._local.task = task
            task.status = "running"
            task._t0 = _dt.datetime.now().timestamp()
            task.started = _dt.datetime.now().strftime("%H:%M:%S")
            task.add("開始")
            self._changed(task)
            result = error = None
            try:
                result = fn()
                task.status = "success"
                task.result = str(result) if result is not None else None
                task.add("完了")
            except Exception as exc:                 # noqa: BLE001 (記録して次へ)
                error = exc
                task.status = "failed"
                task.error = str(exc)
                task.add(f"失敗: {exc}")
                traceback.print_exc()
            finally:
                task.ended = _dt.datetime.now().strftime("%H:%M:%S")
                task.duration = _dt.datetime.now().timestamp() - task._t0
                self._local.task = None
                self._changed(task)
                self._save()                     # 完了状態をディスクに残す
            if on_done is not None:
                try:
                    on_done(result, error)
                except Exception:                    # on_doneの失敗で worker を殺さない
                    traceback.print_exc()

    # ---- 進捗 ----
    def progress(self, text: str) -> None:
        """実行中のジョブから進捗を書く。スレッド毎に持つので並列でも混線しない。"""
        task = getattr(self._local, "task", None)
        if task is not None:
            task.add(text)
            self._changed(task)
        else:
            print("(progress outside job)", text)

    # ---- 参照 ----
    def tasks(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [t.as_dict() for t in self._tasks[:limit]]

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    return t.as_dict()
        return None

    def busy_lanes(self) -> list[str]:
        with self._lock:
            return sorted({t.lane for t in self._tasks if t.status == "running"})

    def clear_finished(self) -> int:
        """終わったタスクの履歴を消す(実行中は残す)。"""
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks
                           if t.status in ("pending", "running")]
            removed = before - len(self._tasks)
        self._save()
        return removed

    # ---- 永続化(GUI/サービス再起動でも履歴を残す) ----
    def _load(self) -> None:
        if not self._persist or not self._persist.exists():
            return
        try:
            data = json.loads(self._persist.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        loaded = []
        maxid = 0
        for d in data:
            t = Task.from_dict(d)
            # 前回サービスが落ちた時に実行中だったものは中断扱いにする(嘘の"実行中"を残さない)
            if t.status in ("running", "pending"):
                t.status = "failed"
                t.error = t.error or "サービス再起動により中断"
                t.add("サービス再起動により中断")
            loaded.append(t)
            try:
                maxid = max(maxid, int(t.id))
            except (TypeError, ValueError):
                pass
        self._tasks = loaded[:MAX_TASKS]
        self._ids = itertools.count(maxid + 1)   # ID衝突を避ける

    def _save(self) -> None:
        if not self._persist:
            return
        with self._lock:
            data = [t.as_dict() for t in self._tasks[:PERSIST_TASKS]]
        try:
            tmp = self._persist.with_suffix(self._persist.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._persist)       # 原子的に置き換え(壊れたファイルを残さない)
        except OSError as exc:
            print("tasks.json 保存失敗:", exc)

    def _changed(self, task: Task) -> None:
        if self._on_task_change:
            try:
                self._on_task_change(task)
            except Exception:
                traceback.print_exc()


# ---- レーン名の決め方(同じ資源への操作を直列化するため) ----
def ark_lane(map_label: str) -> str:
    """ARKの1マップ = 1レーン。そのマップの更新/バックアップ/再起動は直列になる。"""
    return f"ark:{map_label}"


def server_lane(name: str) -> str:
    """MC/Palworld の1サーバー = 1レーン。"""
    return f"server:{name}"


PLAYERS_LANE = "players"   # プレイヤーデータBK(セーブを読むだけ)。更新と並列でよい
