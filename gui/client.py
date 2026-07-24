"""常駐サービスのAPIクライアント。各GUI(別exe)はこれ経由でサービスと話す。

GUIはもう自分でPowerShell/SSH/RCONを叩かない。状態はサービスが持っているので、
GUIは起動した瞬間から最新を表示でき、閉じてもサービス側の処理は止まらない。

依存は標準ライブラリのみ(urllib)。GUIスレッドを止めないよう、呼び出しは
ワーカースレッドから行うこと(各GUIの submit ヘルパを使う)。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8770"


class ServiceUnavailable(Exception):
    """サービスが起動していない/応答しない。GUIは案内を出して再接続を促す。"""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class Client:
    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 15.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    # ---- 低レベル ----
    def _call(self, method: str, path: str, body: dict | None = None,
              timeout: float | None = None):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                msg = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
            except Exception:
                msg = str(exc)
            raise ApiError(exc.code, msg) from exc
        except urllib.error.URLError as exc:
            raise ServiceUnavailable(
                f"GSMサービスに接続できません({self.base})。"
                f"サービスが起動しているか確認してください: {exc.reason}") from exc

    def get(self, path: str, **kw):
        return self._call("GET", path, **kw)

    def post(self, path: str, body: dict | None = None, **kw):
        return self._call("POST", path, body or {}, **kw)

    # ---- 便利メソッド ----
    def health(self) -> dict:
        return self.get("/api/health", timeout=5)

    def alive(self) -> bool:
        try:
            self.health()
            return True
        except Exception:
            return False

    # ARK
    def ark(self) -> list[dict]:
        return self.get("/api/ark")["ark"]

    def ark_meta(self) -> dict:
        return self.get("/api/ark")

    def ark_start(self, idx: int) -> dict:
        return self.post(f"/api/ark/{idx}/start")

    def ark_stop(self, idx: int) -> dict:
        return self.post(f"/api/ark/{idx}/stop")

    def ark_restart(self, idx: int, respawn_dinos: bool = False) -> dict:
        return self.post(f"/api/ark/{idx}/restart", {"respawn_dinos": respawn_dinos})

    def ark_quick(self, idx: int, action: str) -> dict:
        return self.post(f"/api/ark/{idx}/quick", {"action": action})

    def ark_rename(self, idx: int, name: str) -> dict:
        return self.post(f"/api/ark/{idx}/rename", {"name": name})

    def ark_rcon(self, idx: int, cmd: str) -> str:
        return self.post(f"/api/ark/{idx}/rcon", {"cmd": cmd}, timeout=30)["response"]

    def ark_backup(self, idx: int) -> dict:
        return self.post(f"/api/ark/{idx}/backup")

    def ark_update(self, idx: int) -> dict:
        return self.post(f"/api/ark/{idx}/update")

    def ark_batch(self, action: str, indices: list[int],
                  rolling: bool = True) -> dict:
        return self.post("/api/ark/batch",
                         {"action": action, "indices": indices, "rolling": rolling})

    def ark_colors(self) -> dict:
        return self.get("/api/ark/colors")

    def ark_colors_set(self, enabled: bool, colorset: str,
                       respawn: bool = False) -> dict:
        return self.post("/api/ark/colors",
                         {"enabled": enabled, "colorset": colorset,
                          "respawn": respawn})

    def ark_behavior(self) -> dict:
        return self.get("/api/ark/behavior")

    def ark_behavior_set(self, respawn_on_restart: bool) -> dict:
        return self.post("/api/ark/behavior",
                         {"respawn_on_restart": respawn_on_restart})

    def ark_players_backup(self) -> dict:
        return self.post("/api/ark/players-backup")

    def ark_player_backups(self) -> list[dict]:
        return self.get("/api/ark/player-backups")["backups"]

    def ark_player_backup_players(self, file: str) -> list[dict]:
        from urllib.parse import quote
        return self.get(f"/api/ark/player-backup?file={quote(file)}")["players"]

    def ark_players_restore(self, file: str, entries=None, safety: bool = True) -> dict:
        return self.post("/api/ark/players-restore",
                         {"file": file, "entries": entries, "safety": safety})

    # サーバー(MC/Palworld)
    def servers(self) -> list[dict]:
        return self.get("/api/servers")["servers"]

    def server_action(self, name: str, action: str) -> dict:
        return self.post(f"/api/servers/{name}/{action}")

    # ---- 外部公開(UPnP) ----
    def server_publish(self, name: str, unpublish: bool = False) -> dict:
        return self.post(f"/api/servers/{name}/publish", {"unpublish": unpublish})

    # ---- バックアップ/復元 ----
    def ark_backups(self, idx: int) -> list[dict]:
        return self.get(f"/api/ark/{idx}/backups")["backups"]

    def ark_restore(self, idx: int, file: str) -> dict:
        return self.post(f"/api/ark/{idx}/restore", {"file": file})

    def server_backups(self, name: str) -> list[dict]:
        return self.get(f"/api/servers/{name}/backups")["backups"]

    def server_backup(self, name: str) -> dict:
        return self.post(f"/api/servers/{name}/backup", {})

    def server_reset_world(self, name: str, new_seed: str = "", backup: bool = True) -> dict:
        return self.post(f"/api/servers/{name}/reset-world",
                         {"new_seed": new_seed or None, "backup": backup})

    def server_restore(self, name: str, file: str) -> dict:
        return self.post(f"/api/servers/{name}/restore", {"file": file})

    # ---- MC Mod管理 ----
    def mods_list(self, name: str) -> list[dict]:
        return self.get(f"/api/servers/{name}/mods", timeout=60)["mods"]

    def mods_search(self, name: str, query: str, mcver: str,
                    source: str = "modrinth") -> list[dict]:
        return self.post(f"/api/servers/{name}/mods/search",
                         {"query": query, "mcver": mcver, "source": source},
                         timeout=60)["results"]

    def mods_install(self, name: str, source: str, mod_id, mcver: str,
                     restart: bool = True) -> dict:
        return self.post(f"/api/servers/{name}/mods/install",
                         {"source": source, "mod_id": mod_id, "mcver": mcver,
                          "restart": restart})

    def mods_remove(self, name: str, names: list[str], restart: bool = True) -> dict:
        return self.post(f"/api/servers/{name}/mods/remove",
                         {"names": names, "restart": restart})

    def mods_check_updates(self, name: str, mcver: str) -> list[dict]:
        return self.post(f"/api/servers/{name}/mods/check-updates",
                         {"mcver": mcver}, timeout=120)["updates"]

    # ---- MC server.properties ----
    def mc_config_get(self, name: str) -> list[dict]:
        return self.get(f"/api/servers/{name}/serverconfig", timeout=60)["props"]

    def mc_config_set(self, name: str, changes: dict, restart: bool = False) -> dict:
        return self.post(f"/api/servers/{name}/serverconfig",
                         {"changes": changes, "restart": restart})

    def ark_settings_get(self, keys: list[str]) -> dict:
        return self.get("/api/ark/settings?" + ",".join(keys))

    def ark_settings_set(self, changes: dict, all_maps: bool = True) -> dict:
        return self.post("/api/ark/settings",
                         {"changes": changes, "all_maps": all_maps})

    def ark_rawconfig_get(self, file: str) -> dict:
        return self.get(f"/api/ark/rawconfig?file={file}")

    def ark_rawconfig_set(self, file: str, text: str, all_maps: bool = True) -> dict:
        return self.post("/api/ark/rawconfig",
                         {"file": file, "text": text, "all_maps": all_maps})

    def pal_config_get(self, name: str, keys: list[str]) -> dict:
        from urllib.parse import quote
        return self.get(f"/api/servers/{name}/palconfig?" + ",".join(keys),
                        timeout=60)

    def pal_config_set(self, name: str, changes: dict, restart: bool = False) -> dict:
        return self.post(f"/api/servers/{name}/palconfig",
                         {"changes": changes, "restart": restart})

    def server_update_check(self, name: str) -> dict:
        return self.post(f"/api/servers/{name}/update-check", {}, timeout=300)

    def server_update(self, name: str) -> dict:
        return self.post(f"/api/servers/{name}/update", {})

    def server_rcon(self, name: str, cmd: str) -> str:
        return self.post(f"/api/servers/{name}/rcon", {"cmd": cmd},
                         timeout=30)["response"]

    # VM(Hyper-V)
    def provision_templates(self) -> list[dict]:
        return self.get("/api/provision/templates")["templates"]

    def provision(self, **body) -> dict:
        return self.post("/api/provision", body, timeout=60)

    def vms(self) -> list[dict]:
        return self.get("/api/vms", timeout=30)["vms"]

    def vm_clone(self, **body) -> dict:
        return self.post("/api/vms/clone", body, timeout=60)

    def vm_start(self, name: str) -> dict:
        return self.post(f"/api/vms/{name}/start")

    def vm_stop(self, name: str, force: bool = False) -> dict:
        return self.post(f"/api/vms/{name}/stop", {"force": force})

    # タスク
    def tasks(self, limit: int = 100) -> list[dict]:
        return self.get(f"/api/tasks?limit={limit}")["tasks"]

    def task(self, task_id: str) -> dict:
        return self.get(f"/api/tasks/{task_id}")

    def tasks_clear(self) -> dict:
        return self.post("/api/tasks/clear")

    # 予約
    def schedules(self) -> list[dict]:
        return self.get("/api/schedules")["schedules"]

    def save_schedules(self, schedules: list[dict]) -> dict:
        return self.post("/api/schedules", {"schedules": schedules})

    def run_schedule(self, sid: str) -> dict:
        return self.post(f"/api/schedules/{sid}/run")

    # 動的設定
    def dynconfig(self) -> dict:
        return self.get("/api/dynconfig")

    def set_dynconfig(self, values: dict | None = None, enabled: bool | None = None,
                      apply: bool = False, respawn: bool = False) -> dict:
        body: dict = {"apply": apply, "respawn": respawn}
        if values is not None:
            body["values"] = values
        if enabled is not None:
            body["enabled"] = enabled
        return self.post("/api/dynconfig", body, timeout=60)

    # ---- Discord通知(複数送信先) ----
    def notify_get(self) -> dict:
        return self.get("/api/notify")

    def notify_save(self, config: dict) -> dict:
        return self.post("/api/notify", config)

    def notify_test(self, webhook_url: str, text: str = "") -> dict:
        return self.post("/api/notify/test",
                         {"webhook_url": webhook_url, "text": text}, timeout=15)
