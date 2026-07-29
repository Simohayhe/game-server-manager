"""アクション(構築・削除・変更など)を投げたあと、裏のタスクが成功/失敗するまで
その画面内で見届けるための共通ヘルパ。

多くの操作は POST が {"task_id": ...} を即返し、実処理はサービスのジョブレーンで
非同期に走る(status: pending/running/success/failed)。この watch_job は task_id を
ポーリングして、完了(成功/失敗)まで on_update で知らせる。ポーリングは widget.after()
で行うのでGUIもワーカーもブロックしない。

on_update(state, task) の state:
  submitted … 受付(task_id取得)
  running   … 実行中(task['log'] の末尾が進捗)
  success   … 成功(task['result'])
  failed    … 失敗(task['error'])
  error     … 投げる/取得そのものが失敗(task['error'])
"""
from __future__ import annotations


def _last_log(task) -> str:
    log = (task or {}).get("log") or []
    return log[-1] if log else ""


def watch_job(widget, worker, submit_fn, task_fn, on_update, poll_ms: int = 1200):
    """submit_fn()→{task_id} を投げ、task_fn(tid) で完了までポーリングして on_update を呼ぶ。

    task_fn は task_id を受けてタスクdict(status/log/error/result)を返す関数
    (通常 client.task)。
    """

    def got(res, err):
        if not widget.winfo_exists():
            return
        if err:
            on_update("error", {"error": str(err)})
            return
        tid = (res or {}).get("task_id") if isinstance(res, dict) else None
        if not tid:
            # task_id が無い = 同期的に完了した(即成功)
            on_update("success", {"result": res})
            return
        on_update("submitted", {"task_id": tid})
        _schedule(widget, worker, task_fn, tid, on_update, poll_ms)

    worker.submit(submit_fn, got)


def _schedule(widget, worker, task_fn, tid, on_update, poll_ms):
    def once():
        if not widget.winfo_exists():
            return

        def done(task, err):
            if not widget.winfo_exists():
                return
            if err:
                # 一時的な取得失敗は続行(サービス再起動中など)
                on_update("running", {"log": [f"状態取得中… ({err})"]})
                widget.after(poll_ms, once)
                return
            st = (task or {}).get("status")
            if st in ("success", "failed"):
                on_update(st, task)
            else:
                on_update("running", task)
                widget.after(poll_ms, once)
        worker.submit(lambda: task_fn(tid), done)

    widget.after(poll_ms, once)
