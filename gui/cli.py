"""GSM コンソール(CLI)。GUIと同じAPIを叩く軽量なターミナル操作。

  python main_app.py --cli status
  python main_app.py --cli start minecraft4
  python main_app.py --cli restart ark:6
  python main_app.py --cli rcon minecraft4 list
  別PCから: --url http://<host>:8770 --token <token> を付ける

対象(target)は サーバー名 / ARKの "ark:<idx>" / マップ名 のいずれかで指定できる。
"""
from __future__ import annotations

import argparse
import time


def run_cli(url: str, argv: list[str]) -> int:
    from gui.client import ApiError, Client, ServiceUnavailable
    c = Client(url, timeout=60)

    p = argparse.ArgumentParser(prog="gsm --cli", description="GSM コンソール")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="全体の状態")
    sub.add_parser("servers", aliases=["ls"], help="MC/Palworld 一覧")
    sub.add_parser("ark", help="ARK マップ一覧")
    sub.add_parser("vms", help="VM 一覧")
    sub.add_parser("players", help="接続中プレイヤー")
    for name in ("start", "stop", "restart"):
        sp = sub.add_parser(name, help=f"{name} 対象を{name}")
        sp.add_argument("target", help="サーバー名 / ark:<idx> / マップ名")
    rp = sub.add_parser("rcon", help="RCONコマンド送信")
    rp.add_argument("target")
    rp.add_argument("command", nargs="+")
    args = p.parse_args(argv)

    if not args.cmd:
        p.print_help()
        return 0
    try:
        return _dispatch(c, args)
    except ServiceUnavailable as e:
        print("✗ サービスに接続できません:", e)
        return 2
    except ApiError as e:
        print("✗ エラー:", e.message)
        return 1


def _dispatch(c, args) -> int:
    if args.cmd == "status":
        return _status(c)
    if args.cmd in ("servers", "ls"):
        return _servers(c)
    if args.cmd == "ark":
        return _ark(c)
    if args.cmd == "vms":
        return _vms(c)
    if args.cmd == "players":
        return _players(c)
    if args.cmd in ("start", "stop", "restart"):
        return _action(c, args.cmd, args.target)
    if args.cmd == "rcon":
        return _rcon(c, args.target, " ".join(args.command))
    return 0


# ---- 表示 ----
def _status(c) -> int:
    h = c.get("/api/health")
    mode = "直接(VMなし)" if h.get("direct") else "Hyper-V"
    print(f"● GSM サービス: {'稼働中' if h.get('ok') else '不明'}  "
          f"モード={mode}  サーバー={h.get('servers')}  ARK={h.get('ark_maps')}")
    for cc in h.get("ip_conflicts") or []:
        print(f"  ⚠ IP競合: {cc['ip']} ({' / '.join(cc['servers'])})")
    print()
    _servers(c)
    _ark(c, running_only=True)
    return 0


def _servers(c) -> int:
    print("== MC / Palworld ==")
    for s in c.get("/api/servers")["servers"]:
        st = {"active": "🟢稼働", "inactive": "⚪停止"}.get(s.get("status"), "…")
        n = s.get("player_count")
        who = f"  {n}人" if isinstance(n, int) and n else ""
        print(f"  {st}  {s['display_name']}  ({s['name']}){who}")
    return 0


def _ark(c, running_only: bool = False) -> int:
    print("== ARK ==")
    for a in c.get("/api/ark")["ark"]:
        if running_only and not a.get("running"):
            continue
        if a.get("running"):
            st = "🟢稼働" if a.get("ready") else "🟡起動中"
        else:
            st = "⚪停止"
        n = a.get("player_count")
        who = f"  {n}人" if isinstance(n, int) and n else ""
        print(f"  [{a['index']}] {st}  {a['display_name']}{who}")
    return 0


def _vms(c) -> int:
    print("== VM ==")
    for v in c.get("/api/vms").get("vms", []):
        print(f"  {v.get('state','?'):8} {v.get('name')}  {v.get('ip','')}")
    return 0


def _players(c) -> int:
    d = c.get("/api/players")
    print(f"== プレイヤー(合計 {d.get('total', 0)}人) ==")
    for g in d.get("groups", []):
        if not g.get("running"):
            continue
        names = ", ".join(g.get("players") or []) or "(誰もいません)"
        print(f"  {g['display']}: {names}")
    return 0


# ---- 操作 ----
def _resolve(c, target: str):
    """target を ("server", name) か ("ark", idx) に解決。"""
    t = target.strip()
    for s in c.get("/api/servers")["servers"]:
        if t.lower() in (s["name"].lower(), (s.get("display_name") or "").lower()):
            return ("server", s["name"])
    ark = c.get("/api/ark")["ark"]
    if t.lower().startswith("ark:"):
        try:
            return ("ark", int(t.split(":", 1)[1]))
        except ValueError:
            return (None, None)
    for a in ark:
        disp = a["display_name"].lower()
        short = disp.replace("ark: ", "").replace("ark:", "")
        if t == str(a["index"]) or t.lower() in (disp, short, a["map_label"].lower()):
            return ("ark", a["index"])
    return (None, None)


def _action(c, action: str, target: str) -> int:
    kind, ref = _resolve(c, target)
    if kind is None:
        print(f"✗ 対象が見つかりません: {target}")
        return 1
    path = (f"/api/servers/{ref}/{action}" if kind == "server"
            else f"/api/ark/{ref}/{action}")
    print(f"▶ {action}: {target} …")
    res = c.post(path)
    tid = res.get("task_id")
    if not tid:
        print("  (即時完了)")
        return 0
    return _watch(c, tid)


def _watch(c, tid: str) -> int:
    """タスクを完了まで追って進捗を表示。"""
    last = ""
    for _ in range(120):          # 最大~12分
        t = c.get(f"/api/tasks/{tid}")
        log = t.get("log") or []
        line = log[-1] if log else ""
        if line and line != last:
            print(f"  {line}")
            last = line
        st = t.get("status")
        if st == "success":
            print("✓ 完了")
            return 0
        if st == "failed":
            print("✗ 失敗:", t.get("error"))
            return 1
        time.sleep(6)
    print("… まだ実行中です(タスクは継続)。gsm --cli status で確認してください")
    return 0


def _rcon(c, target: str, command: str) -> int:
    kind, ref = _resolve(c, target)
    if kind is None:
        print(f"✗ 対象が見つかりません: {target}")
        return 1
    path = f"/api/servers/{ref}/rcon" if kind == "server" else f"/api/ark/{ref}/rcon"
    res = c.post(path, {"cmd": command})
    print(res.get("response", "") or "(応答なし)")
    return 0
