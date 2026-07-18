"""定期再起動スケジューラの永続モデル。

各ジョブ = 対象サーバー(MC/ARK)を毎日決まった時刻(HH:MM)に再起動する予約。
GUIの外に状態を持ち、アプリを再起動しても schedules.json から復元できるようにする。
実際の発火判定・再起動実行はGUI側(tickループ)が行い、ここは「保存」と「発火時刻の判定」だけ担う。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]  # 0=月 .. 6=日(datetime.weekday準拠)


@dataclass
class RestartJob:
    id: str                      # 一意ID(生成はGUI)
    kind: str                    # "ark" | "mc"
    target: str                  # ark=map_label / mc=サーバー名(profile.name)
    display: str                 # 表示名
    times: list[str] = field(default_factory=list)  # ["04:00", "16:00"]
    days: list[int] = field(default_factory=list)    # 実行曜日 0=月..6=日。空=毎日
    enabled: bool = True
    respawn_dinos: bool = False   # 再起動後に野生恐竜をリスポーン(ARKのみ)
    do_backup: bool = False       # バックアップを実行
    do_update: bool = False       # 更新があれば適用(停止→SteamCMD更新→元が稼働中なら起動)
    do_restart: bool = True       # 再起動を実行(バックアップ→更新→再起動の順に実行)
    rolling: bool = False         # ARK全マップ: 1台ずつ順に(前が復帰してから次)
    order: list = field(default_factory=list)  # ローリング順(map_labelの並び)
    interval_min: int = 0         # >0 なら間隔モード(N分毎に定期バックアップ)。時刻/曜日は無視
    keep: int = 0                 # このジョブ専用の保持世代数(0=config.yamlの既定を使う)

    def is_interval(self) -> bool:
        return self.interval_min > 0

    def action_text(self) -> str:
        if self.is_interval():
            return "バックアップ(稼働中)"
        parts = []
        if self.do_backup:
            parts.append("バックアップ")
        if self.do_update:
            parts.append("更新")
        if self.do_restart:
            parts.append("再起動")
        return " → ".join(parts) if parts else "(なし)"

    def times_text(self) -> str:
        if self.is_interval():
            return f"{self.interval_min}分毎"
        return ", ".join(self.times) if self.times else "(なし)"

    def days_text(self) -> str:
        if not self.days or len(self.days) >= 7:
            return "毎日"
        return "".join(WEEKDAY_LABELS[d] for d in sorted(self.days) if 0 <= d <= 6)

    def runs_on(self, weekday: int) -> bool:
        """weekday(0=月..6=日)にこのジョブが動くか。days空=毎日。"""
        return (not self.days) or (weekday in self.days)


def load_jobs(path: str | Path) -> list[RestartJob]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for j in data.get("jobs", []):
        try:
            if "do_backup" in j or "do_restart" in j:
                do_backup = bool(j.get("do_backup", False))
                do_restart = bool(j.get("do_restart", True))
            else:                              # 旧形式 action からの移行
                act = j.get("action", "restart")
                do_backup = (act == "backup")
                do_restart = (act != "backup")
            out.append(RestartJob(
                id=str(j["id"]), kind=j.get("kind", "mc"),
                target=j.get("target", ""), display=j.get("display", ""),
                times=list(j.get("times", [])),
                days=[int(d) for d in j.get("days", []) if 0 <= int(d) <= 6],
                enabled=bool(j.get("enabled", True)),
                respawn_dinos=bool(j.get("respawn_dinos", False)),
                do_backup=do_backup, do_restart=do_restart,
                do_update=bool(j.get("do_update", False)),
                rolling=bool(j.get("rolling", False)),
                order=list(j.get("order", [])),
                interval_min=int(j.get("interval_min", 0) or 0),
                keep=int(j.get("keep", 0) or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_jobs(path: str | Path, jobs: list[RestartJob]) -> None:
    Path(path).write_text(
        json.dumps({"jobs": [asdict(j) for j in jobs]}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def normalize_time(text: str) -> str | None:
    """'4:00' や '04:0' を 'HH:MM' に正規化。不正なら None。"""
    text = text.strip()
    if ":" not in text:
        return None
    hh, _, mm = text.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def due_jobs(jobs: list[RestartJob], now: datetime) -> list[RestartJob]:
    """今 now(分・曜日)に発火すべき有効ジョブ。重複発火の抑止は呼び出し側で行う。"""
    hhmm = now.strftime("%H:%M")
    wd = now.weekday()               # 0=月 .. 6=日
    return [j for j in jobs
            if j.enabled and not j.is_interval()
            and hhmm in j.times and j.runs_on(wd)]
