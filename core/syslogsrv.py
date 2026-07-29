"""syslog受信サーバー(UDP 514)。ルーター等のログを受けてファイル保存＋Discord転送。

きっかけ: ルーターが深夜に勝手に再起動したが、原因を示すログが手元に残っていな
かった(ルーターの内蔵ログは再起動で消えるうえ、見に行かないと気づけない)。
syslogを飛ばしてもらえば、**次からは原因がその場で分かる**。

設計の要点:
- 受け取ったものは全部ファイルに残す(後から追える)。
- Discordへ転送するのは重要度の高いものだけ。ルーターは平常時もよく喋るので、
  全部流すと通知が使い物にならなくなる。
- **同じ文面の連投を抑える**。機器が壊れかけると同じログを毎秒吐くことがあり、
  そのままだとDiscordが埋まる(レート制限もかかる)。

syslogの優先度(PRI) = facility*8 + severity。severityは 0=emerg 〜 7=debug で、
数字が小さいほど重大。
"""
from __future__ import annotations

import datetime as _dt
import re
import socket
import threading
import time

SEVERITY_LABELS = {
    0: "緊急", 1: "警報", 2: "重大", 3: "エラー",
    4: "警告", 5: "注意", 6: "情報", 7: "デバッグ",
}
SEVERITY_EMOJI = {
    0: "🚨", 1: "🚨", 2: "🔥", 3: "❌", 4: "⚠", 5: "ℹ", 6: "ℹ", 7: "🔧",
}
FACILITY_LABELS = {
    0: "kernel", 1: "user", 2: "mail", 3: "daemon", 4: "auth", 5: "syslog",
    6: "lpr", 7: "news", 8: "uucp", 9: "cron", 10: "authpriv", 11: "ftp",
}

DEFAULT_PORT = 514
DEFAULT_MIN_SEVERITY = 4        # 警告以上だけDiscordへ(平常運転のログは流さない)

# 重要度が低くても必ず転送する語。ルーターは「起動しました」を info(6) で出すので、
# 重要度だけで絞ると**一番知りたい再起動のログが落ちる**(実際にBUFFALOはsev6だった)。
ALWAYS_KEYWORDS = (
    "boot up", "booting", "reboot", "restart", "shutdown", "power",
    "firmware", "upgrade", "update complete",
    "watchdog", "panic", "crash", "fatal", "oops",
    "link down", "wan down", "disconnect", "lease of",
)
# 逆に、平常運転で大量に出るので重要度に関わらず転送しない語(ログには残す)。
NEVER_KEYWORDS = (
    "sending ack", "received request", "sending offer", "received discover",
    "had associated", "had deauthenticated", "setkeysdone",
)
DUP_WINDOW_SEC = 300            # 同じ文面はこの秒数まとめる
MAX_FORWARD_PER_MIN = 12        # 転送の上限(壊れた機器の連投でDiscordを埋めない)

_PRI_RE = re.compile(r"^<(\d{1,3})>(.*)$", re.S)
# RFC3164: "Oct 11 22:14:15 host tag: msg"
_3164_RE = re.compile(
    r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(\S+)\s+(.*)$", re.S)
# RFC5424: "1 2026-07-30T00:19:01Z host app pid msgid sd msg"
_5424_RE = re.compile(r"^1\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(?:\S+|\[[^\]]*\])\s*(.*)$", re.S)


def parse(raw: bytes, addr: str) -> dict:
    """syslogメッセージを解析する。壊れていても捨てずに raw のまま返す。"""
    text = raw.decode("utf-8", "replace").strip()
    sev, fac = 6, 1
    body = text
    m = _PRI_RE.match(text)
    if m:
        pri = int(m.group(1))
        sev, fac = pri & 7, pri >> 3
        body = m.group(2).strip()
    host, tag = addr, ""
    m = _5424_RE.match(body)
    if m:
        host, tag, body = m.group(2), m.group(3), m.group(6).strip()
    else:
        m = _3164_RE.match(body)
        if m:
            host, rest = m.group(2), m.group(3)
            if ":" in rest[:48]:            # "tag: message" ならタグを分ける
                tag, _, body = rest.partition(":")
                tag, body = tag.strip(), body.strip()
            else:
                body = rest.strip()
    return {
        "at": time.time(), "from": addr, "host": host, "tag": tag,
        "severity": sev, "facility": fac, "message": body or text,
        "severity_label": SEVERITY_LABELS.get(sev, str(sev)),
        "facility_label": FACILITY_LABELS.get(fac, str(fac)),
    }


class SyslogServer:
    """常駐サービスに登録して使う(start/stop を持つ)。設定が無効なら何もしない。"""

    def __init__(self, ctx, notifier=None):
        self.ctx = ctx
        self.notifier = notifier
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._recent: list[dict] = []            # 直近ログ(API表示用)
        self._lock = threading.Lock()
        self._dup: dict[str, float] = {}         # 文面 -> 最後に転送した時刻
        self._sent_times: list[float] = []       # 転送のレート制限用
        self.received = 0
        self.forwarded = 0

    # ---- 設定 ----
    @property
    def cfg(self):
        return getattr(self.ctx.config, "syslog", None)

    @property
    def enabled(self) -> bool:
        c = self.cfg
        return bool(c and c.enabled)

    def log_path(self):
        from core.paths import app_dir
        d = app_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"syslog-{_dt.date.today():%Y-%m-%d}.log"

    # ---- 常駐 ----
    def start(self) -> None:
        if not self.enabled:
            return
        c = self.cfg
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((c.host, c.port))
            s.settimeout(1.0)
        except OSError as exc:
            print(f"syslog受信を開始できません({c.host}:{c.port}): {exc}")
            return
        self._sock = s
        self._thread = threading.Thread(target=self._loop, name="gsm-syslog",
                                        daemon=True)
        self._thread.start()
        print(f"syslog受信を開始: {c.host}:{c.port}")

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw, addr = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(parse(raw, addr[0]))
            except Exception as exc:            # noqa: BLE001 受信は止めない
                print("syslog処理で例外:", exc)

    # ---- 1件の処理 ----
    def _handle(self, ev: dict) -> None:
        self.received += 1
        with self._lock:
            self._recent.append(ev)
            del self._recent[:-500]             # 直近500件だけ保持
        self._write(ev)
        if self._should_forward(ev):
            self._forward(ev)

    def _write(self, ev: dict) -> None:
        stamp = _dt.datetime.fromtimestamp(ev["at"]).strftime("%Y-%m-%d %H:%M:%S")
        line = (f"{stamp} [{ev['severity_label']}] {ev['host']} "
                f"{ev['tag']}: {ev['message']}\n")
        try:
            with open(self.log_path(), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _should_forward(self, ev: dict) -> bool:
        c = self.cfg
        if not (c and c.discord):
            return False
        low = f"{ev['tag']} {ev['message']}".lower()
        if any(k in low for k in NEVER_KEYWORDS):    # 平常運転の常時ログは出さない
            return False
        important = any(k in low for k in ALWAYS_KEYWORDS)
        if not important and ev["severity"] > c.min_severity:   # 数字が大きい=軽い
            return False
        now = time.time()
        key = f"{ev['host']}|{ev['tag']}|{ev['message'][:120]}"
        last = self._dup.get(key, 0)
        if now - last < DUP_WINDOW_SEC:          # 同じ文面の連投を抑える
            return False
        # 直近1分の転送数で頭打ちにする(壊れた機器の連投対策)
        self._sent_times = [t for t in self._sent_times if now - t < 60]
        if len(self._sent_times) >= MAX_FORWARD_PER_MIN:
            return False
        self._dup[key] = now
        self._sent_times.append(now)
        if len(self._dup) > 500:                 # 古い記録を掃除
            for k, t in list(self._dup.items()):
                if now - t > DUP_WINDOW_SEC:
                    self._dup.pop(k, None)
        return True

    def _forward(self, ev: dict) -> None:
        if not self.notifier:
            return
        emoji = SEVERITY_EMOJI.get(ev["severity"], "•")
        who = ev["host"] or ev["from"]
        tag = f" {ev['tag']}" if ev["tag"] else ""
        text = f"{emoji} [{ev['severity_label']}] {who}{tag}: {ev['message'][:900]}"
        try:
            self.notifier("syslog", text, None)
            self.forwarded += 1
        except Exception:                        # noqa: BLE001
            pass

    # ---- API向け ----
    def recent(self, limit: int = 100, min_severity: int = 7) -> list[dict]:
        with self._lock:
            items = [e for e in self._recent if e["severity"] <= min_severity]
            return items[-limit:][::-1]          # 新しい順

    def status(self) -> dict:
        c = self.cfg
        return {
            "enabled": self.enabled,
            "listening": self._sock is not None,
            "host": c.host if c else "", "port": c.port if c else 0,
            "discord": bool(c and c.discord),
            "min_severity": c.min_severity if c else DEFAULT_MIN_SEVERITY,
            "received": self.received, "forwarded": self.forwarded,
            "log_file": str(self.log_path()) if self.enabled else "",
        }
