"""サーバー状態の監視。StateCacheを埋め、状態変化を検知して通知/クラッシュ復旧を行う。

旧GUIの _ark_refresh / _on_server_status 相当を常駐側へ移したもの。
GUIが無くても状態が更新され続けるので、GUIは起動した瞬間から最新の状態を見られる。

注意(旧実装から引き継ぐ重要な性質):
  - 初回(prev=None)は「変化」とみなさない。起動直後に全サーバーを停止扱いして
    ポートを閉じてしまう事故が過去にあったため(2026-07-11)。
  - 稼働時間は全マップぶんをPowerShell1回でまとめて取る(マップ毎に叩くと重い)。
"""
from __future__ import annotations

import threading
import time

from core import arkupdate
from core.arkhost import uptimes_by_port
from core.players import player_names

ARK_POLL_SEC = 30
SERVER_POLL_SEC = 30
BUILD_POLL_SEC = 6 * 3600          # 最新ビルドの確認は6時間毎(Steamを叩きすぎない)


class Monitor:
    """ARK/MC/Palworld の状態を定期取得して StateCache に入れる。"""

    def __init__(self, ctx, state, notifier=None, on_change=None, history=None,
                 on_ready=None):
        self.ctx = ctx
        self.state = state
        self.notifier = notifier
        self.on_change = on_change          # (key, kind, display, running, ref) 状態変化
        self.on_ready = on_ready            # (key, display) 起動完了(ARKはadvertising)
        self.history = history              # 人数の推移をグラフ用に記録する
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._prev_ark: dict[int, bool] = {}
        self._prev_srv: dict[str, bool] = {}
        self._prev_ready_ark: dict[int, bool] = {}
        self._prev_ready_srv: dict[str, bool] = {}
        self._ark_ready_latch: dict[int, bool] = {}  # 一度advertisingを見たらラッチ
        self._ark_log_size: dict[int, int] = {}      # ログサイズ(再起動=切り詰めの検知用)
        self._prev_names: dict[str, set] = {}   # key -> 前回の接続者名(入退室判定用)
        self._last_build_check = 0.0
        self._pal_update_check: dict[str, float] = {}   # name -> 最終更新確認時刻
        self._last_pubstat = 0.0
        self._vm_list_cache: list[dict] = []

    def start(self) -> None:
        for target, name in ((self._ark_loop, "gsm-mon-ark"),
                             (self._server_loop, "gsm-mon-srv")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    # ---- ARK ----
    def _ark_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_ark()
            except Exception as exc:
                print("ARK監視で例外:", exc)
            self._stop.wait(ARK_POLL_SEC)

    def _poll_ark(self) -> None:
        # 稼働時間は1回のPowerShellで全プロセスぶん取得(マップ毎に叩かない)
        ups = {}
        try:
            if self.ctx.arkhosts:
                ups = uptimes_by_port(self.ctx.arkhosts[0].runner,
                                      self.ctx.arkhosts[0].cfg.process_name)
        except Exception:
            ups = {}
        self._maybe_check_latest_build()
        self._maybe_pubstat()
        for i, ah in enumerate(self.ctx.arkhosts):
            try:
                running = ah.is_running()
            except Exception as exc:
                self.state.set_ark(i, error=str(exc))
                continue
            players = ""
            count = 0
            if running:
                try:
                    players = ah.players()
                    count = ah.num_players()
                except Exception:
                    pass
            prev_running = self._prev_ark.get(i)
            ready = self._ark_ready(
                i, running, prev_running, ah,
                uptime_sec=ups.get(ah.cfg.game_port) if running else None)
            self._diff_players(f"ark:{i}", "ark", ah.cfg.display_name,
                               players if ready else None)   # 起動完了後のみ入退室判定
            self.state.set_ark(
                i, running=running, ready=ready, players=players, player_count=count,
                uptime_sec=ups.get(ah.cfg.game_port) if running else None,
                build=arkupdate.installed_buildid(ah.cfg.install_root),
                version=ah.client_version(),      # クライアント版数(例 92.4)
                display_name=ah.cfg.display_name, map_label=ah.cfg.map_label)
            if self.history is not None:      # 人数の推移(停止中は0として記録)
                self.history.add(f"players:ark:{ah.cfg.map_label}",
                                 count if running else 0)
            self._changed(f"ark:{i}", "ark", ah.cfg.display_name, running,
                          self._prev_ark, i, ah, "ark")
            self._ready_changed(f"ark:{i}", ah.cfg.display_name, ready,
                                self._prev_ready_ark, i, "ark")

    def _maybe_pubstat(self) -> None:
        """外部公開ステータスを60秒ごとに更新(読み取り専用: UPnP一覧+DNS照会)。"""
        if time.time() - self._last_pubstat < 60:
            return
        self._last_pubstat = time.time()
        try:
            from . import pubstat
            res = pubstat.compute(self.ctx)
        except Exception as exc:
            print("公開ステータスの取得に失敗:", exc)
            return
        for name, st in (res.get("servers") or {}).items():
            self.state.set_server(name, public=st)
        for idx, st in (res.get("ark") or {}).items():
            try:
                self.state.set_ark(int(idx), public=st)
            except Exception:
                pass

    def _maybe_check_latest_build(self) -> None:
        if not self.ctx.ark_steamcmd:
            return
        if time.time() - self._last_build_check < BUILD_POLL_SEC:
            return
        self._last_build_check = time.time()
        try:
            latest = arkupdate.latest_buildid(self.ctx.ark_steamcmd)
            self.state.set_meta(latest_build=latest)
        except Exception as exc:
            print("最新ビルド確認に失敗:", exc)

    # ---- MC / Palworld ----
    def _server_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_servers()
            except Exception as exc:
                print("サーバー監視で例外:", exc)
            self._stop.wait(SERVER_POLL_SEC)

    def _vm_states(self) -> dict:
        """Hyper-VのVM状態をまとめて取得。停止中VMへの無駄なSSHを避けるために使う。

        旧GUIの教訓(CLAUDE.md): 停止中VMにもSSHを試すと1台あたり8秒のタイムアウトで
        待たされ、監視が詰まる。先にVM状態を見て、Offなら即「停止」と判断する。
        """
        try:
            vms = self.ctx.hyperv.list_vms()                  # VMInfo(name, state, ...)
            self._vm_list_cache = [
                {"name": v.name, "state": v.state, "cpu": v.cpu_usage,
                 "memory_mb": v.memory_mb, "uptime_sec": v.uptime_sec} for v in vms]
            return {v.name: v.state for v in vms}
        except Exception as exc:
            print("VM状態の取得に失敗(SSH省略なしで続行):", exc)
            return {}

    def _server_version(self, srv) -> str | None:
        """サーバーのゲームバージョンを取得。Palworldは RCON Info から拾う。

        Palworld: 'Welcome to Pal Server[v1.0.1.100619] …' の [v…] 部分。
        頻繁には変わらないので取得できた値をキャッシュし、RCON負荷を増やさない。
        """
        cache = getattr(self, "_ver_cache", None)
        if cache is None:
            cache = self._ver_cache = {}
        if srv.profile.game != "palworld":
            return None
        ver, ts = cache.get(srv.profile.name, (None, 0.0))
        if ver and time.time() - ts < 600:       # 10分キャッシュ(更新後は再取得)
            return ver
        try:
            import re
            info = srv.rcon_command("Info")
            m = re.search(r"\[v?([\d.]+)\]", info or "")
            if m:
                cache[srv.profile.name] = (m.group(1), time.time())
                return m.group(1)
        except Exception:
            pass
        return ver or None

    def _maybe_check_pal_update(self, name, srv) -> None:
        """Palworldの更新を6時間ごとに確認して state に入れる(表示・通知用)。

        SSHでSteamCMDに問い合わせるので頻繁には叩かない。新規に更新ありを検知したら
        Discordへ1回だけ通知する(update イベント)。
        """
        if srv.profile.game != "palworld":
            return
        last = self._pal_update_check.get(name, 0)
        if time.time() - last < BUILD_POLL_SEC:
            return
        self._pal_update_check[name] = time.time()
        try:
            from core import palupdate
            res = palupdate.check(srv.profile)
        except Exception as exc:
            print(f"Palworld更新確認に失敗({name}):", exc)
            return
        prev = (self.state.server_one(name) or {}).get("update") or {}
        self.state.set_server(name, update=res)
        if res.get("update_available") and not prev.get("update_available"):
            if self.notifier:
                try:
                    self.notifier("update",
                                  f"🆕 {srv.profile.display_name} に更新があります "
                                  f"(build {res.get('installed')} → {res.get('latest')})",
                                  "palworld")
                except Exception:
                    pass

    def _poll_servers(self) -> None:
        vms = self._vm_states()
        self.state.set_meta(vms=self._vm_list_cache)   # VM一覧もAPIから即返せるように
        for name, srv in self.ctx.servers.items():
            vm = srv.profile.vm
            # VMが止まっているサーバーはSSHせず即「停止」= 8秒×台数の待ちを回避
            if vm and vms.get(vm) not in (None, "Running"):
                self.state.set_server(name, status="inactive", players="", ready=False,
                                      vm_state=vms.get(vm),
                                      display_name=srv.profile.display_name,
                                      game=srv.profile.game)
                self._changed(f"mc:{name}", "mc", srv.profile.display_name, False,
                              self._prev_srv, name, srv, srv.profile.game)
                self._ready_changed(f"mc:{name}", srv.profile.display_name, False,
                                    self._prev_ready_srv, name, srv.profile.game)
                continue
            try:
                st = srv.status()
            except Exception as exc:
                self.state.set_server(name, status="error", error=str(exc))
                continue
            running = (st == "active")
            players, count = "", None
            if running:
                try:
                    players = srv.players()
                    count = srv.player_count(players)
                except Exception:
                    pass
            self._diff_players(f"mc:{name}", srv.profile.game,
                               srv.profile.display_name,
                               players if running else None)
            self.state.set_server(name, status=st, players=players,
                                  player_count=count, ready=running,
                                  version=self._server_version(srv) if running else None,
                                  display_name=srv.profile.display_name,
                                  game=srv.profile.game)
            self._changed(f"mc:{name}", "mc", srv.profile.display_name, running,
                          self._prev_srv, name, srv, srv.profile.game)
            self._ready_changed(f"mc:{name}", srv.profile.display_name, running,
                                self._prev_ready_srv, name, srv.profile.game)
            self._maybe_check_pal_update(name, srv)   # Palworldの更新確認(6h毎)

    # ---- 入退室の検知(名前差分) ----
    def _diff_players(self, key: str, game: str, display: str,
                      raw: str | None) -> None:
        """前回との接続者名の差分から入室/退室を通知する。

        raw=None(取得失敗/停止中)のときは判定しない。通信失敗を「全員退室」と
        誤検知しないため。初回に見えた面子は既に居た人なので入室通知しない。
        """
        names = player_names(game if game in ("palworld", "ark") else "minecraft", raw)
        if names is None:
            if raw is None:              # 停止中は記録を消す(次回起動時に誤検知しない)
                self._prev_names.pop(key, None)
            return                       # 取得失敗時は前回を保持したまま何もしない
        now = set(names)
        prev = self._prev_names.get(key)
        self._prev_names[key] = now
        if prev is None:
            return                       # 初回=基準にするだけ(既存プレイヤーは通知しない)
        g = game if game in ("palworld", "minecraft") else "ark"
        for who in sorted(now - prev):
            self._notify_player("player_join", "🟢", who, display, g)
        for who in sorted(prev - now):
            self._notify_player("player_leave", "🔴", who, display, g)

    def _notify_player(self, event: str, icon: str, who: str, where: str,
                       game: str) -> None:
        verb = "入室" if event == "player_join" else "退室"
        if self.notifier:
            try:
                self.notifier(event, f"{icon} {who} が {where} に{verb}", game)
            except Exception as exc:
                print("入退室通知で例外:", exc)

    # ---- 起動完了(ready)の判定 ----
    def _ark_ready(self, i: int, running: bool, prev_running, ah,
                   uptime_sec=None) -> bool:
        """ARKが本当に起動完了(advertising for join)しているか。

        プロセスの有無だけでは「起動中」を「稼働中」と誤表示するため、ログの
        advertising 行で判定する。長時間稼働でこの行がログ末尾から消えるので、
        一度見たらラッチして再確認しない。監視開始時点で既に起動中なら稼働中とみなす。
        """
        if not running:
            self._ark_ready_latch[i] = False
            self._ark_log_size[i] = 0
            return False
        # ログが縮んだ=ARKが起動時に切り詰めた=再起動が起きた → ラッチ解除して起動中に戻す。
        # (監視が「停止の一瞬」を取りこぼしても、これで確実に再起動を検知できる)
        size = ah.log_size()
        prev_size = self._ark_log_size.get(i)
        if prev_size is not None and size < prev_size:
            self._ark_ready_latch[i] = False
        self._ark_log_size[i] = size
        if self._ark_ready_latch.get(i):
            return True                  # 既に起動完了を確認済み(ラッチ)
        if prev_running is None:
            # 監視開始時点で既に動いている。advertising済み or 十分長く稼働(=起動は完了済み)
            # なら稼働中。起動直後(uptime小)でまだ未advertisingなら起動中。
            try:
                adv = ah.is_advertising()
            except Exception:
                adv = False
            if adv or (uptime_sec or 0) >= 300:
                self._ark_ready_latch[i] = True
                return True
            return False                 # 起動直後 = 起動中
        if not prev_running:
            # 起動を検知した最初の周期。ログはこの直後にARKが切り詰めるので、まだ
            # 前セッションの advertising 行が残っている恐れがある。この周期では判定せず、
            # 次の周期(=切り詰め後)から advertising を見る(誤って稼働中にしない)。
            return False
        try:
            if ah.is_advertising():      # 2周期目以降: 新しい advertising が出たら完了
                self._ark_ready_latch[i] = True
                return True
        except Exception:
            pass
        return False                     # プロセスは居るが未 advertising = 起動中

    def _ready_changed(self, key, display, ready, prev_map, prev_key,
                       game=None) -> None:
        """起動完了(ready)が False→True になったら on_ready(=起動通知)を呼ぶ。"""
        prev = prev_map.get(prev_key)
        prev_map[prev_key] = ready
        if prev is None or prev == ready or not ready:
            return                       # 初回は基準にするだけ / 停止方向はここでは扱わない
        if self.on_ready:
            try:
                self.on_ready(key, display, game)
            except Exception as exc:
                print("起動完了ハンドラで例外:", exc)

    # ---- 状態変化 ----
    def _changed(self, key, kind, display, running, prev_map, prev_key, ref,
                 game=None) -> None:
        prev = prev_map.get(prev_key)
        prev_map[prev_key] = running
        if prev is None or prev == running:
            return                       # 初回は変化とみなさない(誤検知防止)
        if self.on_change:
            try:
                self.on_change(key, kind, display, running, ref, game)
            except Exception as exc:
                print("状態変化ハンドラで例外:", exc)
