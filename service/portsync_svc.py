"""自動ポート開放(起動中だけ開ける)を常駐側で行う。

旧GUIの _portsync_* 相当。GUIを閉じてもポートの開閉が続くようにする。

過去の重大事故を踏まえた不変条件(絶対に守ること):
  - 状態が未取得のサーバーは対象にしない。起動直後にキャッシュが空の状態で
    「全部停止中」とみなし、稼働中サーバーのポートまで閉じた事故が2026-07-11に発生した。
  - 管理対象は「サーバー設定のポートに一致するもの」だけ。DNS(53)/RDP(3389)等の
    無関係なマッピングには一切触れない。
"""
from __future__ import annotations

import threading

from core import portsync, upnp
from core.paths import app_dir

PORTSYNC_PATH = app_dir() / "portsync.json"
TICK_SEC = 30
GATEWAY_CACHE_SEC = 600


class PortSyncService:
    def __init__(self, ctx, state, notifier=None):
        self.ctx = ctx
        self.state = state
        self.notifier = notifier
        self.enabled = portsync.load_enabled(PORTSYNC_PATH)
        self._gw = None
        self._gw_at = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---- 常駐部品 ----
    def start(self) -> None:
        threading.Thread(target=self._loop, name="gsm-portsync", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(TICK_SEC)
            if self._stop.is_set():
                break
            if self.enabled:
                try:
                    self.reconcile()
                except Exception as exc:
                    print("ポート同期で例外:", exc)

    # ---- ゲートウェイ ----
    def _gateway(self):
        """NATしているルーターをキャッシュ付きで取得(10分でリフレッシュ)。

        このLANは親機(.1)以外もIGD応答するため、prefer_host で親機を指定する
        (グローバルIPを返すのが親機。旧GUIと同じ取り方)。
        """
        import time
        with self._lock:
            if self._gw is not None and time.time() - self._gw_at < GATEWAY_CACHE_SEC:
                return self._gw
            gwaddr = self.ctx.config.network.gateway
            self._gw = upnp.find_gateway(bind_ip=upnp.local_ip_toward(gwaddr),
                                         prefer_host=gwaddr)
            self._gw_at = time.time()
            return self._gw

    # ---- 対象ポート ----
    def _specs(self) -> list:
        """開閉対象。desired=起動中か。状態未取得のサーバーは含めない(誤って閉じないため)。"""
        specs = []
        for name, srv in self.ctx.servers.items():
            p = srv.profile
            cached = self.state.server_one(name)
            if not p.game_port or not cached or cached.get("status") is None:
                continue                                   # 状態未取得はスキップ
            running = (cached.get("status") == "active")
            proto = "UDP" if p.game == "palworld" else "TCP"   # PalworldはUDP
            specs.append(portsync.PortSpec(
                label=f"{p.game}-{name}", ext_port=(p.external_port or p.game_port),
                internal_ip=p.address, internal_port=p.game_port, proto=proto,
                desired=running))
        if self.ctx.arkhosts:
            try:
                host_ip = upnp.local_ip_toward(self.ctx.config.network.gateway)
            except Exception:
                return specs
            for i, ah in enumerate(self.ctx.arkhosts):
                cached = self.state.ark_one(i)
                if not cached or cached.get("running") is None:
                    continue                               # 状態未取得はスキップ
                running = bool(cached.get("running"))
                if ah.cfg.game_port:
                    specs.append(portsync.PortSpec(
                        f"ark-{ah.cfg.map_label}-game", ah.cfg.game_port, host_ip,
                        ah.cfg.game_port, "UDP", running))
                if ah.cfg.query_port:
                    specs.append(portsync.PortSpec(
                        f"ark-{ah.cfg.map_label}-query", ah.cfg.query_port, host_ip,
                        ah.cfg.query_port, "UDP", running))
        return specs

    @staticmethod
    def _manageable(mapping, spec) -> bool:
        """このマッピングをGSMが開閉してよいか。

        GSMが開けた印(gsm-auto-*)か、転送先が対象サーバー自身なら管理対象とみなす
        (手動公開や既存の常時開放も引き継いで開閉できるようにするため)。
        それ以外(他機器/DNS/RDP等)には触れない。
        """
        if mapping is None:
            return False
        if portsync.is_ours(mapping):
            return True
        return str(mapping.get("internal_client") or "") == str(spec.internal_ip)

    # ---- 照合して開閉 ----
    def reconcile(self) -> list[str]:
        if not self.enabled:
            return []
        gw = self._gateway()
        if gw is None:
            return []
        existing = {}
        for m in gw.client.list_port_mappings():
            existing[(str(m.get("external_port")),
                      (m.get("protocol") or "").upper())] = m
        actions = []
        for spec in self._specs():
            m = existing.get((str(spec.ext_port), spec.proto.upper()))
            mine = self._manageable(m, spec)
            try:
                if spec.desired:                  # 起動中 → 開いていること
                    if m is None:
                        upnp.add_mapping(gw, spec.ext_port, spec.internal_ip,
                                         spec.internal_port, spec.proto,
                                         description=spec.desc)
                        actions.append(f"開 {spec.label} {spec.proto}/{spec.ext_port}")
                    elif mine and not portsync.is_ours(m):   # 既存開放を引き継ぐ
                        upnp.delete_mapping(gw, spec.ext_port, spec.proto)
                        upnp.add_mapping(gw, spec.ext_port, spec.internal_ip,
                                         spec.internal_port, spec.proto,
                                         description=spec.desc)
                        actions.append(f"引継 {spec.label} {spec.proto}/{spec.ext_port}")
                else:                             # 停止中 → 閉じていること
                    if mine:
                        upnp.delete_mapping(gw, spec.ext_port, spec.proto)
                        actions.append(f"閉 {spec.label} {spec.proto}/{spec.ext_port}")
            except Exception as exc:              # 1件失敗しても他は続ける
                print(f"ポート操作に失敗({spec.label}):", exc)
        if actions:
            print("ポート同期:", ", ".join(actions))
            if self.notifier:
                self.notifier("port", "🔌 " + " / ".join(actions))
        return actions

    def on_state_change(self) -> None:
        """起動/停止を検知したら即反映(30秒tickを待たない)。"""
        if not self.enabled:
            return
        self.ctx.jobs.submit("🔌 ポート同期", self.reconcile, lane="portsync",
                             category="ポート")

    def set_enabled(self, enabled: bool) -> dict:
        self.enabled = bool(enabled)
        portsync.save_enabled(PORTSYNC_PATH, self.enabled)
        if self.enabled:
            self.on_state_change()
        else:
            self.ctx.jobs.submit("🔌 ポート全閉(無効化)", self.close_all,
                                 lane="portsync", category="ポート")
        return {"enabled": self.enabled}

    def close_all(self) -> list[str]:
        """対象サーバーの開放ポートを全て閉じる(無関係ポートには触れない)。"""
        gw = self._gateway()
        if gw is None:
            return []
        existing = {}
        for m in gw.client.list_port_mappings():
            existing[(str(m.get("external_port")),
                      (m.get("protocol") or "").upper())] = m
        actions = []
        for spec in self._specs():
            m = existing.get((str(spec.ext_port), spec.proto.upper()))
            if self._manageable(m, spec):
                try:
                    upnp.delete_mapping(gw, spec.ext_port, spec.proto)
                    actions.append(f"閉 {spec.label} {spec.proto}/{spec.ext_port}")
                except Exception as exc:
                    print(f"ポート閉鎖に失敗({spec.label}):", exc)
        return actions
