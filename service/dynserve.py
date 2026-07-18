"""動的設定(交配倍率など)の配信を常駐側で持つ。

旧構成ではGUIがHTTP配信(8712)を抱えていたため、GUIを再起動するたびに配信が落ちた。
2026-07-17はコード修正のたびにGUIを落とす必要があり、そのたびに手動でスクリプトに
8712を肩代わりさせていた(ARKは起動引数のURLを見に来るので、落ちていると取得できない)。
常駐側に移すことでGUIとは無関係に配信が続く。

ASAの仕様(実機で確認済み):
  - GameUserSettings.ini に URL を書いても取得しない。
  - 起動引数 -CustomDynamicConfigUrl="http://127.0.0.1:PORT/dynamicconfig.ini"
    -UseDynamicConfig で渡すと取得する。
  - 反映は起動時 + RCON `ForceUpdateDynamicConfig`。
"""
from __future__ import annotations

from core import dynconfig
from core.paths import app_dir

DYNSTATE_PATH = app_dir() / "dynconfig.json"
DYNFILE_PATH = app_dir() / "dynamicconfig.ini"


class DynServe:
    """動的設定の状態保持 + HTTP配信 + 稼働中マップへの即時反映。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.state = dynconfig.load_state(DYNSTATE_PATH)
        self.server = dynconfig.DynConfigServer(DYNFILE_PATH, self.state.port)

    # ---- 常駐部品としての起動/停止 ----
    def start(self) -> None:
        self._write()
        if self.state.enabled:
            self.server.start()
        self._apply_flags()

    def stop(self) -> None:
        try:
            self.server.stop()
        except Exception:
            pass

    # ---- 内部 ----
    def _write(self) -> None:
        dynconfig.write_file(DYNFILE_PATH, self.state.values)

    def _apply_flags(self) -> None:
        """各ARKの起動引数フラグを状態に合わせる(次回起動から効く)。"""
        for ah in self.ctx.arkhosts:
            ah.cfg.use_dynamic_config = self.state.enabled
            ah.cfg.dynamic_config_url = self.server.url if self.state.enabled else ""

    def _save(self) -> None:
        dynconfig.save_state(DYNSTATE_PATH, self.state)

    # ---- API向け ----
    def as_dict(self) -> dict:
        return {
            "enabled": self.state.enabled,
            "port": self.state.port,
            "url": self.server.url,
            "serving": self.server.running,
            "values": dict(self.state.values),
            "settings": [
                {"key": k, "kind": kind, "label": label, "default": default}
                for k, kind, label, default in dynconfig.DYN_SETTINGS
            ],
            # フラグ付きで動いているマップ = 実際に効いているマップ
            "active_maps": [
                ah.cfg.display_name for ah in self.ctx.arkhosts
                if "-UseDynamicConfig" in ah.cfg.effective_launch_args
            ],
        }

    def update(self, body: dict) -> dict:
        """値/ON-OFFを更新し、apply=True なら稼働中マップへ即反映する。"""
        if "enabled" in body:
            want = bool(body["enabled"])
            if want and not self.server.running:
                self.server.start()
            elif not want and self.server.running:
                self.server.stop()
            self.state.enabled = want
        if "values" in body:
            vals = {k: str(v) for k, v in (body["values"] or {}).items()
                    if k in dynconfig.DYN_KEYS}
            self.state.values = vals
        self._write()
        self._save()
        self._apply_flags()
        applied = []
        if body.get("apply"):
            applied = self.force_update_running()
        return dict(self.as_dict(), applied=applied)

    # ---- カラフル野生恐竜(Dynamic Colorsets) ----
    def get_colors(self) -> dict:
        v = self.state.values
        return {
            "enabled": v.get("ActiveEventColors", "").lower() == "custom",
            "colorset": v.get("DynamicColorset", ""),
            "dyn_enabled": self.state.enabled,   # dynamic config自体が有効か
        }

    def set_colors(self, on: bool, colorset: str, apply: bool = True) -> dict:
        """色設定を既存の動的設定に「混ぜて」書く(交配倍率などは消さない)。"""
        vals = dict(self.state.values)
        if on:
            vals["ActiveEventColors"] = "custom"
            vals["DynamicColorset"] = (colorset or "").strip()
        else:                                    # OFF=色キーを外す(通常色に戻る)
            vals.pop("ActiveEventColors", None)
            vals.pop("DynamicColorset", None)
        self.state.values = vals
        self._write()
        self._save()
        applied = self.force_update_running() if apply else []
        return dict(self.get_colors(), applied=applied)

    def force_update_running(self) -> list[str]:
        """稼働中マップに RCON ForceUpdateDynamicConfig を送って即反映させる。"""
        done = []
        for ah in self.ctx.arkhosts:
            try:
                if ah.is_running():
                    ah.rcon_command("ForceUpdateDynamicConfig")
                    done.append(ah.cfg.display_name)
            except Exception:
                pass                     # 1台失敗しても他は続ける
        return done
