"""APIのエンドポイント定義。Context/StateCache/JobQueue をHTTPに繋ぐ層。

ここには「HTTPの都合」だけを書く。実処理は core/ と service/ にあるので、
将来Web版(FastAPI)を作る時もこのファイルだけ書き換えればよい。
"""
from __future__ import annotations

from core import arkupdate, backup
from core.arkhost import format_uptime

from .api import ApiError, Router
from .runner import PLAYERS_LANE, ark_lane, server_lane


def build_router(ctx, state, scheduler=None, dynserve=None, portsync=None,
                 recovery=None, history=None, notifier=None) -> Router:
    r = Router()
    jobs = ctx.jobs

    def mark(kind: str, key: str) -> None:
        """GSM自身の停止/再起動に印を付ける(クラッシュ自動復旧の誤発火防止)。"""
        if recovery:
            (recovery.mark_restart if kind == "restart" else recovery.mark_stop)(key)

    # ARK再起動時の挙動(恐竜リスポーンON/OFF)。scheduler と同じ arkbehavior.json を見る。
    from core.paths import app_dir as _app_dir
    _behavior_path = _app_dir() / "arkbehavior.json"

    def _respawn_flag() -> bool:
        import json as _json
        try:
            return bool(_json.loads(
                _behavior_path.read_text(encoding="utf-8")).get(
                    "respawn_on_restart", False))
        except (OSError, ValueError):
            return False

    def _notify(event: str, text: str, game: str | None = None) -> None:
        if notifier:
            try:
                notifier(event, text, game)
            except Exception:
                pass

    def _ark_update_map(ah) -> str:
        """1マップを更新し、Discordへ 開始/完了(build・ver付き) を通知する。"""
        latest = arkupdate.latest_buildid(ctx.ark_steamcmd)
        cur = arkupdate.installed_buildid(ah.cfg.install_root)
        name = ah.cfg.display_name
        if cur == latest:
            return f"{name}: 最新({cur})"
        _notify("update", f"⬆ {name} の更新を開始します (build {cur} → {latest})", "ark")
        running = ah.is_running()
        if running:
            jobs.progress(f"■ {name} 更新のため停止…")
            ah.stop_with_notice(progress=jobs.progress)
        try:
            jobs.progress(f"⬆ {name} 更新中…")
            new = arkupdate.update(ctx.ark_steamcmd, ah.cfg.install_root,
                                   progress=jobs.progress)
        finally:
            if running:
                jobs.progress(f"▶ {name} 起動…")
                ah.start(progress=jobs.progress)
                ah.wait_ready(progress=jobs.progress)
        ver = ah.client_version() if running else None     # 版数は起動後のログから
        vtxt = f" / バージョン {ver}" if ver else ""
        _notify("update", f"✅ {name} を更新しました (build {cur} → {new}{vtxt})", "ark")
        return f"{name}: {cur} → {new}"

    def _int_arg(query: str, name: str, default: int) -> int:
        for kv in (query or "").split("&"):
            if kv.startswith(name + "="):
                try:
                    return int(kv.split("=", 1)[1])
                except ValueError:
                    return default
        return default

    # ---------------- 基本 ----------------
    def health(**_):
        return {
            "ok": True,
            "ark_maps": len(ctx.arkhosts),
            "servers": len(ctx.servers),
            "state_age_sec": round(state.age(), 1),
            "busy_lanes": jobs.busy_lanes(),
        }
    r.add("GET", "/api/health", health)

    def reload_cfg(**_):
        ctx.reload()
        return {"ok": True, "ark_maps": len(ctx.arkhosts), "servers": len(ctx.servers)}
    r.add("POST", "/api/reload", reload_cfg)

    # ---------------- ARK ----------------
    def ark_list(**_):
        out = []
        for i, ah in enumerate(ctx.arkhosts):
            cached = state.ark_one(i) or {}
            out.append({
                "index": i,
                "display_name": ah.cfg.display_name,
                "map_label": ah.cfg.map_label,
                "game_port": ah.cfg.game_port,
                "running": cached.get("running"),
                "ready": cached.get("ready"),
                "players": cached.get("players"),
                "player_count": cached.get("player_count"),
                "version": cached.get("version"),
                "uptime_sec": cached.get("uptime_sec"),
                "uptime_text": format_uptime(cached.get("uptime_sec"))
                               if cached.get("running") else "―",
                "build": cached.get("build"),
                "public": cached.get("public"),
                "updated": cached.get("updated"),
            })
        return {"ark": out, "latest_build": state.meta().get("latest_build")}
    r.add("GET", "/api/ark", ark_list)

    def _ark(params):
        ah = ctx.ark_by_index(int(params["idx"]))
        if ah is None:
            raise ApiError(404, f"ARKマップが見つかりません: index={params['idx']}")
        return ah

    def ark_start(params, **_):
        ah = _ark(params)
        t = jobs.submit(f"🦖 起動: {ah.cfg.display_name}",
                        lambda: (ah.start(progress=jobs.progress), "started")[1],
                        lane=ark_lane(ah.cfg.map_label), category="ARK操作")
        return {"task_id": t.id}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/start", ark_start)

    def ark_stop(params, **_):
        ah = _ark(params)
        mark("stop", f"ark:{int(params['idx'])}")     # 意図的な停止=復旧させない
        t = jobs.submit(f"🦖 停止: {ah.cfg.display_name}",
                        lambda: (ah.stop_with_notice(progress=jobs.progress), "stopped")[1],
                        lane=ark_lane(ah.cfg.map_label), category="ARK操作")
        return {"task_id": t.id}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/stop", ark_stop)

    def ark_restart(params, body, **_):
        ah = _ark(params)
        mark("restart", f"ark:{int(params['idx'])}")
        # 明示指定が無ければ arkbehavior.json の設定に従う(手動再起動でもリスポーン)
        respawn = body.get("respawn_dinos")
        respawn = _respawn_flag() if respawn is None else bool(respawn)

        def fn():
            ah.restart_with_notice(respawn_dinos=respawn, progress=jobs.progress)
            ah.wait_ready(progress=jobs.progress)
            return "restarted"
        t = jobs.submit(f"🦖 再起動: {ah.cfg.display_name}", fn,
                        lane=ark_lane(ah.cfg.map_label), category="ARK操作")
        return {"task_id": t.id}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/restart", ark_restart)

    def ark_rename(params, body, **_):
        ah = _ark(params)
        idx = int(params["idx"])
        name = (body.get("name") or "").strip()
        if not name:
            raise ApiError(400, "名前が空です")
        from core import settings
        try:
            settings.set_ark_display_name(ctx.config_path, idx, name)
        except settings.SettingsError as exc:
            raise ApiError(400, str(exc))
        ah.cfg.display_name = name          # 稼働中サービスへ即反映(次の監視でGUI更新)
        return {"ok": True, "display_name": name}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/rename", ark_rename)

    def ark_rcon(params, body, **_):
        ah = _ark(params)
        cmd = (body.get("cmd") or "").strip()
        if not cmd:
            raise ApiError(400, "cmd が空です")
        try:
            return {"response": ah.rcon_command(cmd)}    # RCONは速いので同期でよい
        except Exception as exc:
            raise ApiError(502, f"RCON失敗: {exc}") from exc
    r.add("POST", r"/api/ark/(?P<idx>\d+)/rcon", ark_rcon)

    # クイック操作(保存/リスポーン/昼夜)をタスクとして実行し、画面の📋タスクに残す。
    _ARK_QUICK = {
        "save":    ("💾 保存", "saveworld"),
        "respawn": ("🦕 恐竜リスポーン", None),      # announce付き専用処理
        "day":     ("☀ 昼にする", "settimeofday 09:00"),
        "night":   ("🌙 夜にする", "settimeofday 22:00"),
    }

    def ark_quick(params, body, **_):
        ah = _ark(params)
        action = (body or {}).get("action")
        if action not in _ARK_QUICK:
            raise ApiError(400, f"未知のクイック操作: {action}")
        label, cmd = _ARK_QUICK[action]

        def fn():
            if action == "respawn":                  # 告知付きでリスポーン
                return ah.respawn_wild_dinos_now(progress=jobs.progress)
            jobs.progress(f"{label} を実行中…")
            return ah.rcon_command(cmd)
        t = jobs.submit(f"{label}: {ah.cfg.display_name}", fn,
                        lane=ark_lane(ah.cfg.map_label), category="ARK操作")
        return {"task_id": t.id}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/quick", ark_quick)

    def ark_backup(params, **_):
        ah = _ark(params)

        def fn():
            return backup.ark_backup(
                str(backup.ark_saved_dir(ah.cfg.config_dir)), ctx.backupcfg,
                ah.cfg.map_label, ah.cfg.save_subdir, progress=jobs.progress)
        t = jobs.submit(f"💾 バックアップ: {ah.cfg.display_name}", fn,
                        lane=ark_lane(ah.cfg.map_label), category="バックアップ")
        return {"task_id": t.id}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/backup", ark_backup)

    def ark_update(params, **_):
        ah = _ark(params)
        if not ctx.ark_steamcmd:
            raise ApiError(400, "steamcmd が設定されていません")
        mark("restart", f"ark:{int(params['idx'])}")   # 更新中の停止=意図的

        t = jobs.submit(f"⬆ 更新: {ah.cfg.display_name}",
                        lambda: _ark_update_map(ah),
                        lane=ark_lane(ah.cfg.map_label), category="更新")
        return {"task_id": t.id}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/update", ark_update)

    def ark_settings_get(query, **_):
        """指定キーの現在値を読む。keys=gus:Section:Key,game:Section:Key,... で受ける。

        全マップで設定は共有(config_dir)なので arkhosts[0] を読む。
        """
        if not ctx.arkhosts:
            raise ApiError(400, "ARKマップがありません")
        from core import arkconfig
        gus, game = arkconfig.load(ctx.arkhosts[0].cfg.config_dir)
        out = {}
        for spec in (query or "").split(","):
            parts = spec.split(":")
            if len(parts) != 3:
                continue
            fk, section, key = parts
            ini = gus if fk == "gus" else game
            out[spec] = ini.get(section, key)
        return {"values": out}
    r.add("GET", "/api/ark/settings", ark_settings_get)

    def ark_settings_set(body, **_):
        """変更分を書き込む。all_maps=Trueなら全マップの config_dir に反映(既定)。"""
        from core import arkconfig
        changes = body.get("changes") or {}     # {"fk:section:key": value}
        all_maps = body.get("all_maps", True)
        targets = ctx.arkhosts if all_maps else ctx.arkhosts[:1]
        seen = set()
        for ah in targets:
            cd = str(ah.cfg.config_dir)
            if cd in seen:                       # 同じconfig_dirを共有するマップは1回でよい
                continue
            seen.add(cd)
            gus, game = arkconfig.load(cd)
            for spec, val in changes.items():
                fk, section, key = spec.split(":")
                (gus if fk == "gus" else game).set(section, key, str(val))
            gus.save()
            game.save()
        return {"ok": True, "applied": len(seen), "changed": len(changes)}
    r.add("POST", "/api/ark/settings", ark_settings_set)

    def ark_backups(params, **_):
        ah = _ark(params)
        return {"backups": backup.list_backups(ctx.backupcfg, f"ARK/{ah.cfg.map_label}")}
    r.add("GET", r"/api/ark/(?P<idx>\d+)/backups", ark_backups)

    def ark_restore(params, body, **_):
        ah = _ark(params)
        f = (body or {}).get("file")
        if not f:
            raise ApiError(400, "復元するバックアップ(file)を指定してください")
        if ah.is_running():
            raise ApiError(409, "復元前にこのマップを停止してください")

        def fn():
            backup.ark_restore(f, str(backup.ark_saved_dir(ah.cfg.config_dir)),
                               progress=jobs.progress)
            return "復元しました"
        t = jobs.submit(f"↩ 復元: {ah.cfg.display_name}", fn,
                        lane=ark_lane(ah.cfg.map_label), category="復元")
        return {"task_id": t.id}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/restore", ark_restore)

    def ark_batch(body, **_):
        """複数マップを順番に処理する(ローリング)。同時に1マップしか落ちない/立ち上げない。

        action = start | stop | restart | update。indices=[対象マップのindex]。
        メモリが1マップ分しかないホストでも安全なよう、必ず逐次で回す。
        """
        action = body.get("action")
        indices = [int(i) for i in (body.get("indices") or [])]
        rolling = body.get("rolling", True)
        if action not in ("start", "stop", "restart", "update"):
            raise ApiError(400, f"未知のaction: {action}")
        if not indices:
            raise ApiError(400, "対象(indices)が空です")
        hosts = [(i, ctx.ark_by_index(i)) for i in indices]
        hosts = [(i, h) for i, h in hosts if h is not None]
        if action in ("stop", "restart", "update"):
            for i, _h in hosts:
                mark("restart" if action != "stop" else "stop", f"ark:{i}")

        # 非ローリングの更新: マップごとに別ジョブ(別レーン)で並列に更新する。
        if action == "update" and not rolling:
            if not ctx.ark_steamcmd:
                raise ApiError(400, "steamcmd が設定されていません")

            tids = [jobs.submit(f"⬆ 更新: {ah.cfg.display_name}",
                                (lambda a: lambda: _ark_update_map(a))(ah),
                                lane=ark_lane(ah.cfg.map_label), category="更新").id
                    for _i, ah in hosts]
            return {"task_ids": tids, "parallel": True}

        def fn():
            done, skipped, failed = [], [], []
            for i, ah in hosts:
                name = ah.cfg.display_name
                try:
                    running = ah.is_running()
                    if action == "start":
                        if running:
                            skipped.append(f"{name}(既に稼働)")
                            continue
                        jobs.progress(f"▶ {name} 起動…")
                        ah.start(progress=jobs.progress)
                        ah.wait_ready(progress=jobs.progress)   # 完了を待ってから次へ
                    elif action == "stop":
                        if not running:
                            skipped.append(f"{name}(既に停止)")
                            continue
                        jobs.progress(f"■ {name} 停止…")
                        ah.stop_with_notice(progress=jobs.progress)
                    elif action == "restart":
                        jobs.progress(f"🔁 {name} 再起動…")
                        ah.restart_with_notice(respawn_dinos=_respawn_flag(),
                                               progress=jobs.progress)
                        ah.wait_ready(progress=jobs.progress)
                    elif action == "update":
                        if not ctx.ark_steamcmd:
                            raise RuntimeError("steamcmd 未設定")
                        res = _ark_update_map(ah)     # 通知(build/ver)込み
                        if res.endswith("最新") or "最新(" in res:
                            skipped.append(f"{name}(最新)")
                            continue
                    done.append(name)
                except Exception as exc:
                    failed.append(f"{name}: {exc}")
                    jobs.progress(f"⚠ {name} 失敗: {exc}")
            parts = []
            if done:
                parts.append(f"完了 {len(done)}")
            if skipped:
                parts.append(f"skip {len(skipped)}: " + " / ".join(skipped))
            if failed:
                parts.append(f"失敗 {len(failed)}: " + " / ".join(failed))
            return " ｜ ".join(parts) or "(対象なし)"
        labels = {"start": "▶ 一括起動", "stop": "■ 一括停止",
                  "restart": "🔁 ローリング再起動", "update": "⬆ ローリング更新"}
        t = jobs.submit(f"{labels[action]}({len(hosts)}マップ)", fn,
                        lane="ark-batch", category="ARK操作")
        return {"task_id": t.id}
    r.add("POST", "/api/ark/batch", ark_batch)

    def ark_behavior_get(**_):
        return {"respawn_on_restart": _respawn_flag()}
    r.add("GET", "/api/ark/behavior", ark_behavior_get)

    def ark_behavior_set(body, **_):
        import json as _json
        val = bool((body or {}).get("respawn_on_restart", False))
        try:
            _behavior_path.write_text(
                _json.dumps({"respawn_on_restart": val}), encoding="utf-8")
        except OSError as exc:
            raise ApiError(500, f"保存に失敗: {exc}") from exc
        return {"respawn_on_restart": val}
    r.add("POST", "/api/ark/behavior", ark_behavior_set)

    def ark_rawconfig_get(query, **_):
        """GameUserSettings.ini / Game.ini の生テキストを返す(上級者向け・配列編集用)。"""
        if not ctx.arkhosts:
            raise ApiError(400, "ARKマップがありません")
        from core import arkconfig
        which = "game" if "file=game" in (query or "") else "gus"
        cd = ctx.arkhosts[0].cfg.config_dir
        path = arkconfig.game_path(cd) if which == "game" else arkconfig.gus_path(cd)
        from pathlib import Path
        text = Path(path).read_text(encoding="utf-8", errors="replace") \
            if Path(path).exists() else ""
        return {"file": which, "path": str(path), "text": text}
    r.add("GET", "/api/ark/rawconfig", ark_rawconfig_get)

    def ark_rawconfig_set(body, **_):
        """生テキストをそのまま書き込む(全マップ共通)。配列・任意キーに対応。"""
        from core import arkconfig
        from pathlib import Path
        which = body.get("file") or "gus"
        text = body.get("text")
        if text is None:
            raise ApiError(400, "text がありません")
        all_maps = body.get("all_maps", True)
        targets = ctx.arkhosts if all_maps else ctx.arkhosts[:1]
        seen = set()
        for ah in targets:
            cd = str(ah.cfg.config_dir)
            if cd in seen:
                continue
            seen.add(cd)
            path = (arkconfig.game_path(cd) if which == "game"
                    else arkconfig.gus_path(cd))
            Path(path).write_text(text, encoding="utf-8", newline="")
        return {"ok": True, "applied": len(seen)}
    r.add("POST", "/api/ark/rawconfig", ark_rawconfig_set)

    def ark_players_backup(**_):
        """全マップのプレイヤーデータ+クラスタだけを軽量バックアップ。"""
        entries = [(a.cfg.map_label, str(backup.ark_saved_dir(a.cfg.config_dir)),
                    a.cfg.save_subdir) for a in ctx.arkhosts]
        cluster = ctx.ark_cluster_dir()

        def fn():
            return backup.ark_player_backup(entries, cluster, ctx.backupcfg,
                                            progress=jobs.progress)
        t = jobs.submit("🧬 プレイヤーデータBK", fn, lane=PLAYERS_LANE,
                        category="バックアップ")
        return {"task_id": t.id}
    r.add("POST", "/api/ark/players-backup", ark_players_backup)

    def _players_entries():
        return [(a.cfg.map_label, str(backup.ark_saved_dir(a.cfg.config_dir)),
                 a.cfg.save_subdir) for a in ctx.arkhosts]

    def ark_player_backups(**_):
        """プレイヤーデータBK(players_*.zip)の世代一覧。"""
        return {"backups": backup.list_backups(ctx.backupcfg, "ARK/_players")}
    r.add("GET", "/api/ark/player-backups", ark_player_backups)

    def ark_player_backup_players(query, **_):
        """あるBK内のプレイヤー一覧(名前解決付き)。?file=<path>"""
        from urllib.parse import parse_qs
        f = (parse_qs(query or "").get("file") or [None])[0]
        if not f:
            raise ApiError(400, "file を指定してください")
        return {"players": backup.ark_players_in_backup(f)}
    r.add("GET", "/api/ark/player-backup", ark_player_backup_players)

    def ark_players_restore(body, **_):
        """プレイヤーデータを復元する。body: {file, entries:[...]|null, safety:true}。

        entries=null で全体復元。復元対象マップが稼働中なら 409(停止を促す)。
        復元前に現在の状態を安全BKする(既定)。
        """
        f = (body or {}).get("file")
        entries = (body or {}).get("entries")          # None=全体
        if not f:
            raise ApiError(400, "復元するBK(file)を指定してください")
        label_to_root = {a.cfg.map_label: str(backup.ark_saved_dir(a.cfg.config_dir))
                         for a in ctx.arkhosts}
        # 対象マップの稼働チェック(プロファイルはマップ停止中に戻すのが確実)
        if entries is None:
            involved = set(label_to_root)
        else:
            involved = {e.split("/", 1)[0] for e in entries}
        running = [a.cfg.display_name for a in ctx.arkhosts
                   if a.cfg.map_label in involved and a.is_running()]
        if running:
            raise ApiError(409, "復元前に停止してください: " + "、".join(running))
        cluster = ctx.ark_cluster_dir()
        safety = (body or {}).get("safety", True)

        def fn():
            if safety:
                jobs.progress("復元前に現在のプレイヤーデータを安全バックアップ中…")
                backup.ark_player_backup(_players_entries(), cluster, ctx.backupcfg,
                                         progress=jobs.progress)
            n = backup.ark_player_restore(f, label_to_root, cluster,
                                          entries=entries, progress=jobs.progress)
            return f"{n} ファイルを復元しました"
        t = jobs.submit("↩ プレイヤーデータ復元", fn, lane=PLAYERS_LANE,
                        category="復元")
        return {"task_id": t.id}
    r.add("POST", "/api/ark/players-restore", ark_players_restore)

    # ---------------- MC / Palworld ----------------
    def server_list(**_):
        out = []
        for name, srv in ctx.servers.items():
            cached = state.server_one(name) or {}
            out.append({
                "name": name,
                "display_name": srv.profile.display_name,
                "game": srv.profile.game,
                "vm": srv.profile.vm,
                "address": srv.profile.address,
                "fqdn": srv.profile.fqdn,
                "status": cached.get("status"),
                "ready": cached.get("ready"),
                "players": cached.get("players"),
                "player_count": cached.get("player_count"),
                "version": cached.get("version"),
                "update": cached.get("update"),
                "public": cached.get("public"),
                "updated": cached.get("updated"),
            })
        return {"servers": out}
    r.add("GET", "/api/servers", server_list)

    def _srv(params):
        s = ctx.servers.get(params["name"])
        if s is None:
            raise ApiError(404, f"サーバーが見つかりません: {params['name']}")
        return s

    def server_action(params, **_):
        srv = _srv(params)
        act = params["action"]
        if act not in ("start", "stop", "restart"):
            raise ApiError(400, f"未知の操作: {act}")
        if act in ("stop", "restart"):        # 意図的な操作=クラッシュ復旧させない
            mark("stop" if act == "stop" else "restart", f"mc:{params['name']}")
        # 停止/再起動はプレイヤーへ予告(MC=say / Palworld=Broadcast)してから
        fn_map = {"start": srv.start,
                  "stop": lambda: srv.stop_with_notice(progress=jobs.progress),
                  "restart": lambda: srv.restart_with_notice(progress=jobs.progress)}
        labels = {"start": "▶ 起動", "stop": "■ 停止", "restart": "🔁 再起動"}
        t = jobs.submit(f"{labels[act]}: {srv.profile.display_name}",
                        lambda: (fn_map[act](), act)[1],
                        lane=server_lane(params["name"]), category="サーバー操作")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/(?P<action>start|stop|restart)",
          server_action)

    def server_rcon(params, body, **_):
        srv = _srv(params)
        cmd = (body.get("cmd") or "").strip()
        if not cmd:
            raise ApiError(400, "cmd が空です")
        try:
            return {"response": srv.rcon_command(cmd)}
        except Exception as exc:
            raise ApiError(502, f"RCON失敗: {exc}") from exc
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/rcon", server_rcon)

    def server_publish(params, body, **_):
        """MC/Palworldを外部公開(UPnP転送 + DNS)。unpublish=Trueで停止。"""
        srv = _srv(params)
        from service import pubstat
        stop = bool((body or {}).get("unpublish"))

        def fn():
            if stop:
                pubstat.unpublish_server(ctx, srv.profile)
                return "公開を停止しました"
            wan = pubstat.publish_server(ctx, srv.profile)
            return f"公開しました (WAN {wan} / 接続名 {srv.profile.fqdn})"
        label = "🚫 公開停止" if stop else "🌍 外部公開"
        t = jobs.submit(f"{label}: {srv.profile.display_name}", fn,
                        lane=server_lane(params["name"]), category="外部公開")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/publish", server_publish)

    def server_backups(params, **_):
        _srv(params)
        return {"backups": backup.list_backups(ctx.backupcfg, params["name"])}
    r.add("GET", r"/api/servers/(?P<name>[^/]+)/backups", server_backups)

    def server_backup(params, **_):
        srv = _srv(params)
        fn = backup.pal_backup if srv.profile.game == "palworld" else backup.mc_backup
        t = jobs.submit(f"💾 バックアップ: {srv.profile.display_name}",
                        lambda: fn(srv.profile, ctx.backupcfg, progress=jobs.progress),
                        lane=server_lane(params["name"]), category="バックアップ")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/backup", server_backup)

    def server_restore(params, body, **_):
        srv = _srv(params)
        f = (body or {}).get("file")
        if not f:
            raise ApiError(400, "復元するバックアップ(file)を指定してください")
        rest = backup.pal_restore if srv.profile.game == "palworld" else backup.mc_restore
        mark("restart", f"mc:{params['name']}")

        def fn():
            rest(srv.profile, f, progress=jobs.progress)
            return "復元しました"
        t = jobs.submit(f"↩ 復元: {srv.profile.display_name}", fn,
                        lane=server_lane(params["name"]), category="復元")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/restore", server_restore)

    def server_palconfig_get(params, query, **_):
        """Palworldの現在設定を読む。keys=Key1,Key2,... で指定キーの値を返す。"""
        srv = _srv(params)
        if srv.profile.game != "palworld":
            raise ApiError(400, "Palworldのみ対応です")
        from core import palconfig
        try:
            opts = palconfig.read(srv.profile)
        except Exception as exc:
            raise ApiError(502, f"設定の取得に失敗: {exc}") from exc
        keys = [k for k in (query or "").split(",") if k]
        return {"values": {k: opts.get(k) for k in keys}}
    r.add("GET", r"/api/servers/(?P<name>[^/]+)/palconfig", server_palconfig_get)

    def server_palconfig_set(params, body, **_):
        """Palworldの設定を書き込む(変更分のみ)。restart=Trueで再起動して反映。"""
        srv = _srv(params)
        if srv.profile.game != "palworld":
            raise ApiError(400, "Palworldのみ対応です")
        from core import palconfig
        changes = body.get("changes") or {}
        restart = bool(body.get("restart", False))
        if restart:
            mark("restart", f"mc:{params['name']}")

        def fn():
            opts = palconfig.read(srv.profile)
            for k, v in changes.items():
                opts.set(k, str(v))
            palconfig.write(srv.profile, opts, restart=restart, progress=jobs.progress)
            return f"{len(changes)}項目を保存"
        t = jobs.submit(f"⚙ 設定保存: {srv.profile.display_name}", fn,
                        lane=server_lane(params["name"]), category="設定変更")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/palconfig", server_palconfig_set)

    # ---- MC Mod管理(Modrinth / CurseForge) ----
    def _cfkey():
        return getattr(ctx.config, "curseforge_api_key", "") or ""

    def mods_list(params, **_):
        srv = _srv(params)
        if srv.profile.game != "minecraft":
            raise ApiError(400, "Mod管理はMinecraftのみ対応です")
        from core import modmanager
        try:
            return {"mods": modmanager.list_installed_meta(srv.profile)}
        except Exception as exc:
            raise ApiError(502, f"Mod一覧の取得に失敗: {exc}") from exc
    r.add("GET", r"/api/servers/(?P<name>[^/]+)/mods", mods_list)

    def mods_search(params, body, **_):
        _srv(params)
        from core import onlinemods
        q = (body.get("query") or "").strip()
        mcver = (body.get("mcver") or "").strip()
        source = body.get("source") or "modrinth"
        if not q or not mcver:
            raise ApiError(400, "query と mcver が必要です")
        try:
            return {"results": onlinemods.search(q, mcver, _cfkey(),
                                                 sources=(source,))}
        except Exception as exc:
            raise ApiError(502, f"検索に失敗: {exc}") from exc
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/mods/search", mods_search)

    def mods_install(params, body, **_):
        srv = _srv(params)
        from core import modmanager, onlinemods
        source = body.get("source") or "modrinth"
        mod_id = body.get("mod_id")
        mcver = (body.get("mcver") or "").strip()
        restart = bool(body.get("restart", True))
        if not mod_id or not mcver:
            raise ApiError(400, "mod_id と mcver が必要です")

        def fn():
            jobs.progress("依存を解決中…")
            plan = onlinemods.collect_with_deps(source, mod_id, mcver, _cfkey())
            warnings = plan.pop("__warnings__", [])
            for w in warnings:
                jobs.progress("⚠ " + w)
            entries = list(plan.values())     # 本体＋必須依存
            return modmanager.install_online(srv.profile, entries, restart=restart,
                                             progress=jobs.progress)
        t = jobs.submit(f"🧩 Mod導入: {srv.profile.display_name}", fn,
                        lane=server_lane(params["name"]), category="Mod管理")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/mods/install", mods_install)

    def mods_remove(params, body, **_):
        srv = _srv(params)
        from core import modmanager
        names = body.get("names") or []
        restart = bool(body.get("restart", True))
        if not names:
            raise ApiError(400, "削除するmod(names)を指定してください")
        t = jobs.submit(f"🧩 Mod削除: {srv.profile.display_name}",
                        lambda: modmanager.remove_mods(srv.profile, names,
                                                       restart=restart,
                                                       progress=jobs.progress),
                        lane=server_lane(params["name"]), category="Mod管理")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/mods/remove", mods_remove)

    def mods_check_updates(params, body, **_):
        srv = _srv(params)
        from core import modmanager
        mcver = (body.get("mcver") or "").strip()
        if not mcver:
            raise ApiError(400, "mcver が必要です")
        try:
            return {"updates": modmanager.check_updates_modrinth(srv.profile, mcver)}
        except Exception as exc:
            raise ApiError(502, f"更新確認に失敗: {exc}") from exc
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/mods/check-updates", mods_check_updates)

    def server_config_get(params, **_):
        """MCの server.properties を全キー読む(順序保持)。"""
        srv = _srv(params)
        if srv.profile.game != "minecraft":
            raise ApiError(400, "server.propertiesの編集はMinecraftのみ対応です")
        from core import serverconfig
        try:
            text = serverconfig.read_config(srv.profile)
        except Exception as exc:
            raise ApiError(502, f"設定の取得に失敗: {exc}") from exc
        props = serverconfig.Properties(text)
        return {"props": [{"key": k, "value": props.get(k)} for k in props.keys()]}
    r.add("GET", r"/api/servers/(?P<name>[^/]+)/serverconfig", server_config_get)

    def server_config_set(params, body, **_):
        srv = _srv(params)
        if srv.profile.game != "minecraft":
            raise ApiError(400, "Minecraftのみ対応です")
        from core import serverconfig
        changes = body.get("changes") or {}
        restart = bool(body.get("restart", False))
        if restart:
            mark("restart", f"mc:{params['name']}")

        def fn():
            text = serverconfig.read_config(srv.profile)
            props = serverconfig.Properties(text)
            for k, v in changes.items():
                props.set(k, str(v))
            serverconfig.write_config(srv.profile, props.text(), restart=restart,
                                      progress=jobs.progress)
            return f"{len(changes)}項目を保存"
        t = jobs.submit(f"⚙ 設定保存: {srv.profile.display_name}", fn,
                        lane=server_lane(params["name"]), category="設定変更")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/serverconfig", server_config_set)

    def server_update_check(params, **_):
        """Palworldの更新有無を今すぐ確認(SSHでSteamCMDに問い合わせ)。"""
        srv = _srv(params)
        if srv.profile.game != "palworld":
            raise ApiError(400, "更新確認はPalworldのみ対応です")
        from core import palupdate
        try:
            res = palupdate.check(srv.profile)
        except Exception as exc:
            raise ApiError(502, f"更新確認に失敗: {exc}") from exc
        state.set_server(params["name"], update=res)     # 一覧の表示にも反映
        return res
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/update-check", server_update_check)

    def server_update(params, **_):
        """Palworldを更新(停止→SteamCMD→起動)。"""
        srv = _srv(params)
        if srv.profile.game != "palworld":
            raise ApiError(400, "更新はPalworldのみ対応です")
        from core import palupdate
        mark("restart", f"mc:{params['name']}")          # 更新中の停止=意図的
        t = jobs.submit(f"⬆ 更新: {srv.profile.display_name}",
                        lambda: palupdate.update(srv.profile, progress=jobs.progress),
                        lane=server_lane(params["name"]), category="更新")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/update", server_update)

    # ---------------- ログ(ライブ表示用) ----------------
    def ark_log(params, query, **_):
        """since=<byte offset> を渡すと増分だけ返す(ライブ表示を軽く・速くするため)。"""
        ah = _ark(params)
        n = _int_arg(query, "lines", 300)
        since = _int_arg(query, "since", 0)
        try:
            text, offset = ah.tail_log_since(offset=since, lines=n)
            return {"log": text, "offset": offset, "append": bool(since),
                    "path": str(ah.cfg.log_path)}
        except Exception as exc:
            raise ApiError(502, f"ログ取得に失敗: {exc}") from exc
    r.add("GET", r"/api/ark/(?P<idx>\d+)/log", ark_log)

    def server_log(params, query, **_):
        srv = _srv(params)
        n = _int_arg(query, "lines", 300)
        try:
            return {"log": srv.tail_log(n)}
        except Exception as exc:
            raise ApiError(502, f"ログ取得に失敗: {exc}") from exc
    r.add("GET", r"/api/servers/(?P<name>[^/]+)/log", server_log)

    # ---------------- 履歴(グラフ用) ----------------
    if history is not None:
        def hist(query, **_):
            keys = None
            for kv in (query or "").split("&"):
                if kv.startswith("keys="):
                    keys = [k for k in kv.split("=", 1)[1].split(",") if k]
            data = history.all()
            if keys:
                data = {k: v for k, v in data.items() if k in keys}
            return {"history": data}
        r.add("GET", "/api/history", hist)

    # ---------------- VM(Hyper-V) ----------------
    def vm_list(**_):
        cached = state.meta().get("vms")
        if cached is None:                    # 未取得ならその場で1回だけ取る
            cached = [{"name": v.name, "state": v.state, "cpu": v.cpu_usage,
                       "memory_mb": v.memory_mb, "uptime_sec": v.uptime_sec}
                      for v in ctx.hyperv.list_vms()]
            state.set_meta(vms=cached)
        # そのVM上のサーバー名を添える(どのVMを止めると何が落ちるか分かるように)
        by_vm: dict[str, list[str]] = {}
        for name, srv in ctx.servers.items():
            if srv.profile.vm:
                by_vm.setdefault(srv.profile.vm, []).append(srv.profile.display_name)
        for v in cached:
            v["servers"] = by_vm.get(v["name"], [])
        return {"vms": cached}
    r.add("GET", "/api/vms", vm_list)

    def vm_start(params, **_):
        name = params["name"]
        t = jobs.submit(f"🖥 VM起動: {name}",
                        lambda: (ctx.hyperv.start_vm(name), "started")[1],
                        lane=f"vm:{name}", category="VM操作")
        return {"task_id": t.id}
    r.add("POST", r"/api/vms/(?P<name>[^/]+)/start", vm_start)

    def vm_stop(params, body, **_):
        name = params["name"]
        force = bool(body.get("force", False))
        # VMを止めれば上のサーバーも落ちる。意図的なので復旧させない
        for sname, srv in ctx.servers.items():
            if srv.profile.vm == name:
                mark("stop", f"mc:{sname}")

        def fn():
            # VMを止める前に、そのVM上のゲームサービスを安全停止(=ワールド保存)する。
            # systemctl stop は SIGTERM で、MC等はシャットダウンフックで保存してから
            # 終了する(完了までブロック)。これを待たずにVMを落とすとデータが飛ぶ。
            for srv in ctx.servers.values():
                p = srv.profile
                if p.vm != name or not p.service:
                    continue
                try:
                    if srv.status() == "active":
                        jobs.progress(f"{p.display_name}: 保存して停止中…(VM停止前)")
                        srv.stop()
                except Exception as exc:      # 接続不可でもVM停止は続行(ベストエフォート)
                    jobs.progress(f"{p.display_name}: 停止に失敗({exc}) VM停止は続行")
            jobs.progress(f"VM {name} を{'強制' if force else ''}停止…")
            ctx.hyperv.stop_vm(name, force=force)
            return "stopped"
        t = jobs.submit(f"🖥 VM{'強制' if force else ''}停止: {name}", fn,
                        lane=f"vm:{name}", category="VM操作")
        return {"task_id": t.id}
    r.add("POST", r"/api/vms/(?P<name>[^/]+)/stop", vm_stop)

    # ---------------- タスク ----------------
    def task_list(query, **_):
        limit = 100
        for kv in (query or "").split("&"):
            if kv.startswith("limit="):
                try:
                    limit = int(kv.split("=", 1)[1])
                except ValueError:
                    pass
        return {"tasks": jobs.tasks(limit=limit)}
    r.add("GET", "/api/tasks", task_list)

    def task_one(params, **_):
        t = jobs.get_task(params["tid"])
        if t is None:
            raise ApiError(404, "タスクが見つかりません")
        return t
    r.add("GET", r"/api/tasks/(?P<tid>[^/]+)", task_one)

    def task_clear(**_):
        return {"removed": jobs.clear_finished()}
    r.add("POST", "/api/tasks/clear", task_clear)

    # ---------------- 予約 ----------------
    if scheduler is not None:
        def sched_list(**_):
            return {"schedules": scheduler.as_dicts()}
        r.add("GET", "/api/schedules", sched_list)

        def sched_save(body, **_):
            scheduler.replace_all(body.get("schedules") or [])
            return {"ok": True, "count": len(scheduler.as_dicts())}
        r.add("POST", "/api/schedules", sched_save)

        def sched_run(params, **_):
            scheduler.fire_by_id(params["sid"])
            return {"ok": True}
        r.add("POST", r"/api/schedules/(?P<sid>[^/]+)/run", sched_run)

    # ---------------- 設定(ポート同期 / クラッシュ復旧) ----------------
    def settings_get(**_):
        return {
            "portsync_enabled": portsync.enabled if portsync else None,
            "crash_recovery_enabled": recovery.enabled if recovery else None,
        }
    r.add("GET", "/api/settings", settings_get)

    def settings_set(body, **_):
        out = {}
        if portsync is not None and "portsync_enabled" in body:
            out.update(portsync=portsync.set_enabled(body["portsync_enabled"]))
        if recovery is not None and "crash_recovery_enabled" in body:
            out.update(recovery=recovery.set_enabled(body["crash_recovery_enabled"]))
        return dict(settings_get(), **out)
    r.add("POST", "/api/settings", settings_set)

    if portsync is not None:
        def ports_reconcile(**_):
            t = jobs.submit("🔌 ポート同期(手動)", portsync.reconcile, lane="portsync",
                            category="ポート")
            return {"task_id": t.id}
        r.add("POST", "/api/ports/reconcile", ports_reconcile)

    # ---------------- 動的設定 ----------------
    if dynserve is not None:
        def dyn_get(**_):
            return dynserve.as_dict()
        r.add("GET", "/api/dynconfig", dyn_get)

        def dyn_set(body, **_):
            res = dynserve.update(body)
            if body.get("respawn"):        # 色反映のため既存の野生恐竜を置き換える
                def fn():
                    out = []
                    for ah in ctx.arkhosts:
                        try:
                            if ah.is_running():
                                ah.respawn_wild_dinos_now(progress=jobs.progress)
                                out.append(ah.cfg.display_name)
                        except Exception as exc:
                            out.append(f"{ah.cfg.display_name}:失敗({exc})")
                    return "リスポーン: " + (", ".join(out) or "対象なし")
                t = jobs.submit("🎨 色反映リスポーン", fn, lane="ark-batch",
                                category="ARK操作")
                res["respawn_task"] = t.id
            return res
        r.add("POST", "/api/dynconfig", dyn_set)

        # ---- カラフル野生恐竜(Dynamic Colorsets・イベント/mod不要) ----
        def colors_get(**_):
            return dynserve.get_colors()
        r.add("GET", "/api/ark/colors", colors_get)

        def colors_set(body, **_):
            on = bool((body or {}).get("enabled"))
            colorset = (body or {}).get("colorset", "")
            respawn = bool((body or {}).get("respawn"))    # 既存個体も置き換える
            res = dynserve.set_colors(on, colorset, apply=True)
            if respawn:                                    # destroywilddinos は重いのでジョブで
                def fn():
                    out = []
                    for ah in ctx.arkhosts:
                        try:
                            if ah.is_running():
                                ah.respawn_wild_dinos_now(progress=jobs.progress)
                                out.append(ah.cfg.display_name)
                        except Exception as exc:
                            out.append(f"{ah.cfg.display_name}:失敗({exc})")
                    return "リスポーン: " + (", ".join(out) or "対象なし")
                t = jobs.submit("🎨 色反映リスポーン", fn, lane="ark-batch",
                                category="ARK操作")
                res["respawn_task"] = t.id
            return res
        r.add("POST", "/api/ark/colors", colors_set)

    # ---------------- Discord通知(複数送信先) ----------------
    from core import notify
    from core.paths import app_dir
    notify_path = app_dir() / "notify.json"

    def notify_get(**_):
        cfg = notify.load(notify_path)
        return {"config": cfg.to_dict(), "events": notify.EVENT_LABELS,
                "games": notify.GAME_LABELS}
    r.add("GET", "/api/notify", notify_get)

    def notify_set(body, **_):
        cfg = notify.config_from_dict(body or {})     # 検証も兼ねる
        notify.save(notify_path, cfg)                 # サービスは mtime で即読み直す
        return {"config": cfg.to_dict()}
    r.add("POST", "/api/notify", notify_set)

    def notify_test(body, **_):
        url = (body or {}).get("webhook_url", "").strip()
        text = (body or {}).get("text") or "✅ GSM テスト送信"
        if not url:
            raise ApiError(400, "Webhook URLが空です")
        try:
            notify.send(url, text)
        except Exception as exc:
            raise ApiError(502, f"送信に失敗: {exc}") from exc
        return {"ok": True}
    r.add("POST", "/api/notify/test", notify_test)

    return r
