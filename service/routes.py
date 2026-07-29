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
            "direct": bool(getattr(ctx, "direct", False)),
            "state_age_sec": round(state.age(), 1),
            "busy_lanes": jobs.busy_lanes(),
            "ip_conflicts": (state.meta() or {}).get("ip_conflicts") or [],
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

    # ---------------- プレイヤー(接続中の名前一覧) ----------------
    def players_all(**_):
        """全サーバー(MC/Palworld/ARK各マップ)の接続中プレイヤー名をまとめて返す。

        監視キャッシュの players(RCON生出力)を core.players.player_names で名前に変換。
        追加のRCONは叩かない(監視が更新済みのものを使う)。
        """
        from core.players import player_entries
        groups = []
        total = 0
        for name, srv in ctx.servers.items():
            cached = state.server_one(name) or {}
            running = cached.get("status") == "active"
            entries = player_entries(srv.profile.game, cached.get("players")) if running else None
            cnt = len(entries) if entries is not None else cached.get("player_count")
            if entries:
                total += len(entries)
            groups.append({
                "kind": srv.profile.game, "id": name,
                "display": srv.profile.display_name, "running": bool(running),
                "ready": bool(running), "known": entries is not None,
                "count": cnt, "players": [e["name"] for e in (entries or [])],
                "entries": entries or [],
            })
        for i, ah in enumerate(ctx.arkhosts):
            cached = state.ark_one(i) or {}
            running = bool(cached.get("running"))
            ready = bool(cached.get("ready"))
            entries = player_entries("ark", cached.get("players")) if ready else None
            cnt = len(entries) if entries is not None else cached.get("player_count")
            if entries:
                total += len(entries)
            groups.append({
                "kind": "ark", "id": f"ark:{i}",
                "display": ah.cfg.display_name, "running": running,
                "ready": ready, "known": entries is not None,
                "count": cnt, "players": [e["name"] for e in (entries or [])],
                "entries": entries or [],
            })
        return {"groups": groups, "total": total}
    r.add("GET", "/api/players", players_all)

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
        # 進行中の起動待ち/再起動カウントダウンを即中断させる(同レーンで順番待ちに
        # ならないよう、ジョブ投入前に同期でフラグを立てる)=「起動中に停止」対応。
        ah.request_cancel()
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

    def ark_moderate(params, body, **_):
        """ARKのBAN/キック/BAN解除/許可リスト。停止中はBanList.txtを直接編集。"""
        from core import moderation as M
        ah = _ark(params)
        b = body or {}
        action = (b.get("action") or "").strip()
        target = (b.get("target") or "").strip()
        try:
            running = ah.is_running()
        except Exception:
            running = False
        if action == "banlist":                       # 一覧はファイルから(停止中でも可)
            return {"banned": M.ark_banlist_read(ah.cfg.install_root), "running": running}
        if action in ("kick", "ban", "unban", "wl_add", "wl_remove") and not target:
            raise ApiError(400, "対象(EOS ID/名前)を指定してください")
        if not running and action in ("ban", "unban"):   # オフライン=ファイル編集
            try:
                if action == "ban":
                    M.ark_ban_offline(ah.cfg.install_root, target)
                    msg = f"{target} をBANしました(BanList.txtへ追記・次回起動で反映)"
                else:
                    M.ark_unban_offline(ah.cfg.install_root, target)
                    msg = f"{target} のBANを解除しました(BanList.txtから削除)"
            except M.ModerationError as exc:
                raise ApiError(400, str(exc))
            return {"ok": True, "offline": True, "message": msg}
        if not running:
            raise ApiError(409, "この操作にはマップの起動が必要です(停止中はBAN/解除のみ可)")
        try:
            cmd = M.ark_rcon_command(action, target)
        except M.ModerationError as exc:
            raise ApiError(400, str(exc))
        try:
            resp = ah.rcon_command(cmd)
        except Exception as exc:
            raise ApiError(502, f"RCON失敗: {exc}") from exc
        try:                                           # BanList.txtにも反映(一覧整合)
            if action == "ban":
                M.ark_ban_offline(ah.cfg.install_root, target)
            elif action == "unban":
                M.ark_unban_offline(ah.cfg.install_root, target)
        except Exception:
            pass
        return {"ok": True, "offline": False, "response": resp}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/moderate", ark_moderate)

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

    def ark_mapsettings_get(params, **_):
        """マップ固有設定(例 Ragnarokの火山)を、そのマップのconfigから読む。"""
        from core import arkconfig
        ah = _ark(params)
        spec = arkconfig.ARK_MAP_SETTINGS.get(ah.cfg.map_label)
        if not spec:
            return {"map_label": ah.cfg.map_label, "section": None, "settings": []}
        gus, _game = arkconfig.load(ah.cfg.config_dir)
        section = spec["section"]
        out = []
        for key, typ, label, default in spec["items"]:
            cur = gus.get(section, key)
            out.append({"key": key, "type": typ, "label": label,
                        "default": default,
                        "current": cur if cur is not None else ""})
        return {"map_label": ah.cfg.map_label, "section": section, "settings": out}
    r.add("GET", r"/api/ark/(?P<idx>\d+)/mapsettings", ark_mapsettings_get)

    def ark_mapsettings_set(params, body, **_):
        """マップ固有設定を、そのマップのconfigにだけ書く(全マップには広げない)。"""
        from core import arkconfig
        ah = _ark(params)
        spec = arkconfig.ARK_MAP_SETTINGS.get(ah.cfg.map_label)
        if not spec:
            raise ApiError(400, f"{ah.cfg.display_name} に固有設定はありません")
        changes = (body or {}).get("changes") or {}      # {key: value}
        section = spec["section"]
        valid = {k for k, _t, _l, _d in spec["items"]}
        gus, _game = arkconfig.load(ah.cfg.config_dir)
        n = 0
        for key, val in changes.items():
            if key in valid:
                gus.set(section, key, str(val))
                n += 1
        gus.save()
        return {"ok": True, "changed": n, "map_label": ah.cfg.map_label}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/mapsettings", ark_mapsettings_set)

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

    def ark_reset_world(params, body, **_):
        """ARKマップのワールドをリセット(セーブ削除→再生成)。既定で事前バックアップ。

        破壊的。停止→(BK)→セーブ削除→起動。GUI側で強く確認すること。
        """
        import re as _re
        ah = _ark(params)
        idx = int(params["idx"])
        do_backup = (body or {}).get("backup", True)
        m = _re.match(r'"?([A-Za-z0-9_]+)', ah.cfg.launch_args)
        wp = m.group(1) if m else None
        if not wp:
            raise ApiError(400, "マップ名(WP)を特定できません")
        saved_root = str(backup.ark_saved_dir(ah.cfg.config_dir))
        mark("stop", f"ark:{idx}")        # 停止=意図的(クラッシュ復旧させない)

        def fn():
            if do_backup:
                jobs.progress("リセット前に自動バックアップ…")
                backup.ark_backup(saved_root, ctx.backupcfg, ah.cfg.map_label,
                                  ah.cfg.save_subdir, progress=jobs.progress)
            if ah.is_running():
                jobs.progress("停止中…")
                ah.stop(progress=jobs.progress)
            backup.ark_reset_world(saved_root, ah.cfg.save_subdir, wp,
                                   progress=jobs.progress)
            jobs.progress("起動中(新規生成)…")
            ah.start(progress=jobs.progress)
            return "ワールドをリセットしました(新規生成中)"
        t = jobs.submit(f"🔄 ワールドリセット: {ah.cfg.display_name}", fn,
                        lane=ark_lane(ah.cfg.map_label), category="ワールドリセット")
        return {"task_id": t.id}
    r.add("POST", r"/api/ark/(?P<idx>\d+)/reset-world", ark_reset_world)

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

    # ARK季節イベント。ASAはイベントを公式Mod(CurseForge/StudioWildcard)で配布するので、
    # -ActiveEvent ではなく -mods=<ID> で有効化する(arkhost.EVENT_MODS/merge_mods)。
    # value は EVENT_MODS のキーと一致させること。設定のみ=次回GSM起動時に全マップへ反映。
    ARK_EVENTS = [
        {"value": "",                 "label": "なし(通常)"},
        {"value": "Summer",           "label": "Summer Bash(夏)"},
        {"value": "WinterWonderland", "label": "Winter Wonderland(冬)"},
        {"value": "FearEvolved",      "label": "Fear Ascended(ハロウィン)"},
        {"value": "Love",             "label": "Love Ascended(バレンタイン)"},
        {"value": "Easter",           "label": "Eggcellent Adventure(イースター)"},
        {"value": "TurkeyTrial",      "label": "Turkey Trial(感謝祭)"},
    ]
    ARK_EVENT_NOTE = (
        "ASAのイベントは公式Mod(CurseForge)で有効化します。設定すると、そのイベントの"
        "Mod IDを全マップの -mods= に追加します(Astraeos等の既存Modは保持)。"
        "反映は次回のGSM起動時で、初回はサーバーがMODをダウンロードするぶん起動が長くなります"
        "(参加プレイヤー側も自動DL)。イベント色は『新しく湧いた野生恐竜』にだけ付くので、"
        "起動後に『🦕 野生恐竜を今すぐリスポーン(DestroyWildDinos)』を実行すると色が付きます。")

    def ark_event_get(**_):
        return {"event": ctx.ark_event(), "choices": ARK_EVENTS,
                "note": ARK_EVENT_NOTE}
    r.add("GET", "/api/ark/event", ark_event_get)

    def ark_event_set(body, **_):
        ev = ctx.set_ark_event(str((body or {}).get("event", "")))
        return {"event": ev}
    r.add("POST", "/api/ark/event", ark_event_set)

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

        entries=null で全体復元。**マップ停止は不要** — 復元するプロファイルは、その本人が
        オフラインなら稼働中でも安全に差し替えできる(本人が入り直せば反映)。ただし対象プレイヤーが
        今まさに接続中だと saveworld で上書きされ得るので、その場合だけ 409(ログアウトを促す)。
        復元前に現在の状態を安全BKする(既定)。
        """
        import re as _re
        f = (body or {}).get("file")
        entries = (body or {}).get("entries")          # None=全体
        if not f:
            raise ApiError(400, "復元するBK(file)を指定してください")
        label_to_root = {a.cfg.map_label: str(backup.ark_saved_dir(a.cfg.config_dir))
                         for a in ctx.arkhosts}
        # 復元対象のプレイヤーID(ファイル名=EOS ID)。entries=None は全員が対象。
        target_ids = None
        if entries is not None:
            target_ids = {e.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                          for e in entries if e.endswith(".arkprofile")}
        # 稼働中マップの接続中プレイヤーIDを集める(ListPlayersの32桁hex)
        online = {}     # id -> map display
        for a in ctx.arkhosts:
            if not a.is_running():
                continue
            try:
                raw = a.players() or ""
            except Exception:                          # noqa: BLE001
                raw = ""
            for m in _re.finditer(r"([0-9a-fA-F]{32})", raw):
                online[m.group(1).lower()] = a.cfg.display_name
        if target_ids is None:
            if online:
                who = "、".join(sorted(set(online.values())))
                raise ApiError(409, f"接続中のプレイヤーがいます({who})。"
                               "全体復元は全員ログアウト後に行ってください。")
        else:
            conflict = [pid for pid in target_ids if pid.lower() in online]
            if conflict:
                raise ApiError(409, "復元対象のプレイヤーが接続中です。"
                               "本人がログアウトしてから復元してください(マップ停止は不要)。")
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

    # ---------------- 新規サーバー構築(プロビジョニング) ----------------
    def provision_templates(**_):
        """構築テンプレ(Fabric/Forge等)とその既定バージョンを返す。"""
        from core import provision as prov
        return {"templates": [
            {"id": t.id, "label": t.label, "display_name": t.display_name,
             "mc_version": t.mc_version, "description": t.description,
             "game_port": t.defaults.get("game_port", 25565)}
            for t in prov.load_templates()]}
    r.add("GET", "/api/provision/templates", provision_templates)

    def provision_versions(query, **_):
        from urllib.parse import parse_qs
        tid = (parse_qs(query or "").get("template") or [""])[0]
        from core import provision as prov
        return {"versions": prov.available_versions(tid)}
    r.add("GET", "/api/provision/versions", provision_versions)

    def provision_new(body, **_):
        """既存VM(SSH到達可)に指定バージョンのサーバーを構築し、config追記＋反映する。

        body: {template_id, name, display_name, mc_version, host, ssh_user,
               ssh_password, vm?, game_port?, motd?, memory_max_mb?}
        """
        from core import provision as prov
        b = body or {}
        tid = b.get("template_id")
        name = (b.get("name") or "").strip()
        host = (b.get("host") or "").strip()
        ssh_user = b.get("ssh_user")
        ssh_pass = b.get("ssh_password")
        if not all([tid, name, host, ssh_user, ssh_pass]):
            raise ApiError(400, "template_id / name / host / ssh_user / ssh_password は必須です")
        if any(p.name == name for p in ctx.config.servers):
            raise ApiError(409, f"サーバー名 '{name}' は既に存在します")
        tmap = {t.id: t for t in prov.load_templates()}
        t = tmap.get(tid)
        if not t:
            raise ApiError(400, f"不明なテンプレート: {tid}")

        d = dict(t.defaults)
        rcon_pw = prov.generate_password()
        version = (b.get("mc_version") or d.get("mc_version") or "").strip()
        game_port = int(b.get("game_port") or d.get("game_port") or 25565)
        rcon_port = int(d.get("rcon_port") or 25575)
        params = {
            **d,
            "mc_version": version,
            "motd": b.get("motd") or d.get("motd") or "A Minecraft Server",
            "memory_max_mb": b.get("memory_max_mb") or d.get("memory_max_mb") or "auto",
            "game_port": game_port, "rcon_port": rcon_port,
            "service": d.get("service", "minecraft"),
            "install_dir": d.get("install_dir", "/opt/minecraft"),
            "runtime_user": d.get("runtime_user", "minecraft"),
            "ssh_user": ssh_user, "rcon_password": rcon_pw,
        }
        script = prov.render_script(t, params)       # 未指定プレースホルダはここで検出
        display = b.get("display_name") or name
        vm = (b.get("vm") or "").strip()

        # VMも新規に作る場合(テンプレからクローン→個体化してから構築)
        create_vm = bool(b.get("create_vm"))
        vm_template = (b.get("vm_template") or "ubuntu_template").strip()
        template_ip = (b.get("template_ip") or "192.168.11.199").strip()
        try:
            new_mem_mb = int(float(b.get("vm_memory_gb") or 4) * 1024)
            new_cpu = int(b.get("vm_cpu") or 4)
        except (TypeError, ValueError):
            raise ApiError(400, "VMのメモリ/CPUは数値で指定してください")
        if create_vm and not vm:
            raise ApiError(400, "VMを新規作成する場合はVM名が必要です")

        def fn():
            if create_vm:
                from core import orchestration as orch
                net = getattr(ctx.config, "network", None)
                new_ip = net.full_ip(host) if net else host
                gateway = net.gateway if net else "192.168.11.1"
                dns = (ctx.config.dns.host if getattr(ctx.config, "dns", None)
                       else "192.168.11.254")
                jobs.progress(f"{vm_template} を {vm} に複製中…")
                ctx.hyperv.clone_vm(vm_template, vm, new_mem_mb, new_cpu)
                jobs.progress(f"{vm} を起動→SSH応答待ち({template_ip})…")
                ctx.hyperv.start_vm(vm)
                orch._wait_for_port(template_ip, 22, timeout=240)
                jobs.progress(f"個体化(hostname={vm} / IP={new_ip})…再起動します")
                orch.individualize_clone(template_ip, ssh_user, ssh_pass, vm,
                                         new_ip, gateway, dns, progress=jobs.progress)
            jobs.progress(f"{host} に {t.display_name} {version} を構築開始…")
            prov.provision(host, ssh_user, ssh_pass, script, progress=jobs.progress)
            profile: dict = {"display_name": display}
            if vm:
                profile["vm"] = vm
            profile.update({
                "address": host,
                "ssh": {"user": ssh_user, "password": ssh_pass},
                "service": params["service"],
                "rcon": {"port": rcon_port, "password": rcon_pw},
                "game_port": game_port,
                "players_command": t.profile_extra.get("players_command", "list"),
                "version_pattern": t.profile_extra.get("version_pattern"),
                "players_pattern": t.profile_extra.get("players_pattern"),
            })
            # LAN内DNSへA/PTRを自動登録(設定がある時)。fqdnをプロファイルに保存。
            # 失敗しても構築は成功扱い(名前解決はIP直で代替できるため)。
            if getattr(ctx.config, "dns", None) is not None:
                try:
                    from core import dnsreg
                    fqdn = dnsreg.register_host(ctx.config.dns, vm or name, host,
                                                progress=jobs.progress)
                    profile["fqdn"] = fqdn
                except Exception as exc:
                    jobs.progress(f"DNS登録に失敗(続行・IP直で利用可): {exc}")
            prov.append_profile_to_config(ctx.config_path, name, profile)
            ctx.reload()                             # 稼働中サービスに即反映
            return f"{display}({version}) を構築し、一覧に追加しました"
        task = jobs.submit(f"⚙ 新規構築: {display} {version}", fn, category="構築")
        return {"task_id": task.id}
    r.add("POST", "/api/provision", provision_new)

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
        name = params["name"]
        act = params["action"]
        if act not in ("start", "stop", "restart"):
            raise ApiError(400, f"未知の操作: {act}")
        if act in ("stop", "restart"):        # 意図的な操作=クラッシュ復旧させない
            mark("stop" if act == "stop" else "restart", f"mc:{name}")
        from core.orchestration import start_server_with_vm
        # 起動はVMがOffなら先にVM起動→SSH応答待ち→サービス起動(VM自動起動)。
        # 直接モードはVMが無いのでプロセスを直接起動する。
        # 停止/再起動はプレイヤーへ予告(MC=say / Palworld=Broadcast)してから。
        if getattr(ctx, "direct", False):
            start_fn = lambda: srv.start(progress=jobs.progress)
        else:
            start_fn = lambda: start_server_with_vm(ctx.hyperv, srv, progress=jobs.progress)
        fn_map = {
            "start": start_fn,
            "stop": lambda: srv.stop_with_notice(progress=jobs.progress),
            "restart": lambda: srv.restart_with_notice(progress=jobs.progress),
        }
        labels = {"start": "▶ 起動", "stop": "■ 停止", "restart": "🔁 再起動"}

        def job():
            fn_map[act]()
            try:                    # 30秒のポーリング待ちを避け、即座に実状態を反映
                st = srv.status()
                state.set_server(name, status=st, ready=(st == "active"),
                                 display_name=srv.profile.display_name,
                                 game=srv.profile.game)
            except Exception:       # noqa: BLE001
                pass
            return act
        t = jobs.submit(f"{labels[act]}: {srv.profile.display_name}", job,
                        lane=server_lane(name), category="サーバー操作")
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

    def server_moderate(params, body, **_):
        """MC/PalworldのBAN/キック/BAN解除/ホワイトリスト(RCON)。要サーバー起動。"""
        from core import moderation as M
        srv = _srv(params)
        game = srv.profile.game
        b = body or {}
        action = (b.get("action") or "").strip()
        target = (b.get("target") or "").strip()
        reason = (b.get("reason") or "").strip()
        try:
            if game == "minecraft":
                cmd = M.mc_command(action, target, reason)
            elif game == "palworld":
                cmd = M.pal_command(action, target)
            else:
                raise ApiError(400, "このサーバーでは未対応です")
        except M.ModerationError as exc:
            raise ApiError(400, str(exc))
        if action not in ("banlist", "wl_list", "wl_on", "wl_off") and not target:
            raise ApiError(400, "対象を指定してください")
        try:
            resp = srv.rcon_command(cmd)
        except Exception as exc:
            raise ApiError(502, f"RCON失敗(サーバーが起動しているか確認): {exc}") from exc
        return {"ok": True, "command": cmd, "response": resp}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/moderate", server_moderate)

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
        t = jobs.submit(
            f"💾 バックアップ: {srv.profile.display_name}",
            lambda: _with_vm_ssh(
                srv.profile,
                lambda: fn(srv.profile, ctx.backupcfg, progress=jobs.progress),
                jobs.progress),
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
        t = jobs.submit(f"↩ 復元: {srv.profile.display_name}",
                        lambda: _with_vm_ssh(srv.profile, fn, jobs.progress),
                        lane=server_lane(params["name"]), category="復元")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/restore", server_restore)

    # ------------------------------------------------------------------
    # バックアップ統合管理(ゲーム別セクション・世代/日数保持・削除)
    # ------------------------------------------------------------------
    def _profile_by_name(name):
        srv = ctx.servers.get(name)
        return srv.profile if srv else None

    def _ark_idx_by_label(label):
        for i, a in enumerate(ctx.arkhosts):
            if a.cfg.map_label == label:
                return i, a
        return None, None

    def _bk_meta(target):
        """target(保存先のサブパス表記) から {game, display, kind, idx?} を求める。"""
        if target == "ARK/_players":
            return {"game": "ark", "display": "プレイヤーデータ(全マップ)",
                    "kind": "players", "idx": None}
        if target.startswith("ARK/"):
            label = target.split("/", 1)[1]
            i, a = _ark_idx_by_label(label)
            disp = a.cfg.display_name if a else label
            return {"game": "ark", "display": disp, "kind": "world", "idx": i}
        prof = _profile_by_name(target)
        if prof:
            return {"game": prof.game, "display": prof.display_name,
                    "kind": "world", "idx": None}
        return {"game": "other", "display": target, "kind": "world", "idx": None}

    def backups_all(**_):
        """保存先を丸ごと走査し、ゲーム別に整理して返す。"""
        scanned = backup.scan_all(ctx.backupcfg)
        targets = []
        for target, bks in scanned.items():
            meta = _bk_meta(target)
            targets.append({
                "target": target, "display": meta["display"], "game": meta["game"],
                "kind": meta["kind"], "idx": meta.get("idx"), "count": len(bks),
                "total_mb": round(sum(b["size_mb"] for b in bks), 1),
                "backups": bks,
            })
        targets.sort(key=lambda t: (t["game"], t["display"].lower()))
        return {"targets": targets}
    r.add("GET", "/api/backups", backups_all)

    def backup_delete(body, **_):
        f = (body or {}).get("file")
        if not f:
            raise ApiError(400, "削除するバックアップ(file)を指定してください")
        try:
            backup.delete_backup(ctx.backupcfg, f)
        except backup.BackupError as exc:
            raise ApiError(400, str(exc))
        return {"deleted": f}
    r.add("POST", "/api/backups/delete", backup_delete)

    def backup_restore_any(body, **_):
        """target 種別に応じて適切な復元を行う(統合管理画面用)。"""
        b = body or {}
        target = b.get("target")
        f = b.get("file")
        if not target or not f:
            raise ApiError(400, "target と file を指定してください")
        if target == "ARK/_players":
            label_to_root = {a.cfg.map_label: str(backup.ark_saved_dir(a.cfg.config_dir))
                             for a in ctx.arkhosts}
            cluster = ctx.ark_cluster_dir()

            def fn():
                n = backup.ark_player_restore(f, label_to_root, cluster,
                                              progress=jobs.progress)
                return f"復元しました({n}ファイル)"
            t = jobs.submit("↩ プレイヤーデータ復元", fn,
                            lane=PLAYERS_LANE, category="復元")
            return {"task_id": t.id}
        if target.startswith("ARK/"):
            label = target.split("/", 1)[1]
            idx, ah = _ark_idx_by_label(label)
            if ah is None:
                raise ApiError(404, f"ARKマップ '{label}' が見つかりません")
            if ah.is_running():
                raise ApiError(409, "復元前にこのマップを停止してください")

            def fn():
                backup.ark_restore(f, str(backup.ark_saved_dir(ah.cfg.config_dir)),
                                   progress=jobs.progress)
                return "復元しました"
            t = jobs.submit(f"↩ 復元: {ah.cfg.display_name}", fn,
                            lane=ark_lane(ah.cfg.map_label), category="復元")
            return {"task_id": t.id}
        prof = _profile_by_name(target)
        if not prof:
            raise ApiError(404, f"サーバー '{target}' が見つかりません(削除済み?)")
        srv = ctx.servers[target]
        rest = backup.pal_restore if prof.game == "palworld" else backup.mc_restore
        mark("restart", f"mc:{target}")

        def fn():
            rest(prof, f, progress=jobs.progress)
            return "復元しました"
        t = jobs.submit(f"↩ 復元: {prof.display_name}",
                        lambda: _with_vm_ssh(prof, fn, jobs.progress),
                        lane=server_lane(target), category="復元")
        return {"task_id": t.id}
    r.add("POST", "/api/backups/restore", backup_restore_any)

    def backup_settings_get(**_):
        c = ctx.backupcfg
        return {"path": c.path, "keep": c.keep, "players_keep": c.players_keep,
                "retention_days": c.retention_days, "compress": c.compress}
    r.add("GET", "/api/backups/settings", backup_settings_get)

    def backup_settings_set(body, **_):
        b = body or {}
        upd = {}
        for k in ("path", "keep", "players_keep", "retention_days", "compress"):
            if k in b and b[k] is not None:
                upd[k] = b[k]
        if not upd:
            raise ApiError(400, "変更する項目がありません")
        for k in ("keep", "players_keep", "retention_days"):   # 数値は非負整数に
            if k in upd:
                try:
                    upd[k] = max(0, int(upd[k]))
                except (TypeError, ValueError):
                    raise ApiError(400, f"{k} は整数で指定してください")
        from core import settings
        settings.update_config(ctx.config_path, {"backup": upd})
        ctx.reload()
        return {"saved": upd}
    r.add("POST", "/api/backups/settings", backup_settings_set)

    # ------------------------------------------------------------------
    # 設定のエクスポート/インポート(暗号化対応)
    # ------------------------------------------------------------------
    def config_export(body, **_):
        import base64 as _b64
        from core import configio
        from core.paths import app_dir as _ad
        b = body or {}
        with_secrets = bool(b.get("with_secrets", True))
        password = str(b.get("password") or "")
        try:
            data, fname = configio.export_bundle(
                _ad(), with_secrets=with_secrets, password=password)
        except configio.ConfigIOError as exc:
            raise ApiError(400, str(exc))
        return {"filename": fname, "encrypted": bool(password),
                "data": _b64.b64encode(data).decode("ascii")}
    r.add("POST", "/api/config/export", config_export)

    def config_peek(body, **_):
        import base64 as _b64
        from core import configio
        b = body or {}
        try:
            blob = _b64.b64decode(b.get("data") or "")
        except Exception:
            raise ApiError(400, "データを読み取れませんでした")
        enc = configio.is_encrypted(blob)
        if enc and not b.get("password"):
            return {"encrypted": True, "need_password": True}
        try:
            manifest = configio.peek(blob, str(b.get("password") or ""))
        except configio.ConfigIOError as exc:
            raise ApiError(400, str(exc))
        return {"encrypted": enc, "need_password": False, "manifest": manifest}
    r.add("POST", "/api/config/peek", config_peek)

    def config_import(body, **_):
        import base64 as _b64
        from core import configio
        from core.paths import app_dir as _ad
        b = body or {}
        try:
            blob = _b64.b64decode(b.get("data") or "")
        except Exception:
            raise ApiError(400, "データを読み取れませんでした")

        def job():
            try:
                return configio.import_bundle(
                    _ad(), blob, str(b.get("password") or ""),
                    progress=jobs.progress)
            except configio.ConfigIOError as exc:
                raise ApiError(400, str(exc))
            finally:
                try:
                    ctx.reload()
                except Exception:
                    pass
        t = jobs.submit("⬇ 設定をインポート", job, category="設定")
        return {"task_id": t.id}
    r.add("POST", "/api/config/import", config_import)

    # ---------------- ホストPCの再起動(再起動後に元へ復帰) ----------------
    import threading as _threading
    _host_restart_cancel = _threading.Event()

    def host_restart(body, **_):
        """予告→カウントダウン(取消可)→ARK保存停止→PC再起動。VMはHyper-Vが自動復帰。"""
        from core import hostpower
        b = body or {}
        try:
            delay = max(0, int(b.get("delay_sec", 60)))
        except (TypeError, ValueError):
            raise ApiError(400, "delay_sec は数値で指定してください")
        _host_restart_cancel.clear()

        def job():
            return hostpower.restart_host(
                ctx, delay_sec=delay, progress=jobs.progress,
                is_cancelled=_host_restart_cancel.is_set)
        t = jobs.submit("🖥 PC再起動", job, category="ホスト")
        return {"task_id": t.id}
    r.add("POST", "/api/host/restart", host_restart)

    def host_restart_cancel(**_):
        _host_restart_cancel.set()
        return {"cancelled": True}
    r.add("POST", "/api/host/restart/cancel", host_restart_cancel)

    def server_reset_world(params, body, **_):
        """MCのワールドをリセット(削除→再生成)。既定でリセット前に自動バックアップ。

        body: {new_seed?, backup?(既定True)}。破壊的なのでGUI側で強く確認すること。
        """
        srv = _srv(params)
        name = params["name"]
        game = srv.profile.game
        if game not in ("minecraft", "palworld"):
            raise ApiError(400, "ワールドリセットはMinecraft/Palworldのみ対応です")
        seed = (body or {}).get("new_seed") or None
        do_backup = (body or {}).get("backup", True)
        mark("restart", f"mc:{name}")     # リセット中の停止=意図的(クラッシュ復旧させない)

        def fn():
            if do_backup:
                jobs.progress("リセット前に自動バックアップ中…")
                bk = backup.pal_backup if game == "palworld" else backup.mc_backup
                bk(srv.profile, ctx.backupcfg, progress=jobs.progress)
            if game == "palworld":
                backup.pal_reset_world(srv.profile, progress=jobs.progress)
            else:
                backup.mc_reset_world(srv.profile, new_seed=seed, progress=jobs.progress)
            try:                          # 状態を即反映
                st = srv.status()
                state.set_server(name, status=st, ready=(st == "active"))
            except Exception:             # noqa: BLE001
                pass
            return "ワールドをリセットしました(新規生成中)"
        t = jobs.submit(f"🔄 ワールドリセット: {srv.profile.display_name}", fn,
                        lane=server_lane(name), category="ワールドリセット")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/reset-world", server_reset_world)

    def server_palconfig_get(params, query, **_):
        """Palworldの現在設定を読む。keys=Key1,Key2,... で指定キーの値を返す。"""
        srv = _srv(params)
        if srv.profile.game != "palworld":
            raise ApiError(400, "Palworldのみ対応です")
        from core import palconfig
        try:
            opts = _with_vm_ssh(srv.profile, lambda: palconfig.read(srv.profile))
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
        t = jobs.submit(f"⚙ 設定保存: {srv.profile.display_name}",
                        lambda: _with_vm_ssh(srv.profile, fn, jobs.progress),
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
            return {"mods": _with_vm_ssh(
                srv.profile, lambda: modmanager.list_installed_meta(srv.profile))}
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
        t = jobs.submit(f"🧩 Mod導入: {srv.profile.display_name}",
                        lambda: _with_vm_ssh(srv.profile, fn, jobs.progress),
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
        t = jobs.submit(
            f"🧩 Mod削除: {srv.profile.display_name}",
            lambda: _with_vm_ssh(
                srv.profile,
                lambda: modmanager.remove_mods(srv.profile, names, restart=restart,
                                               progress=jobs.progress),
                jobs.progress),
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
            return {"updates": _with_vm_ssh(
                srv.profile,
                lambda: modmanager.check_updates_modrinth(srv.profile, mcver))}
        except Exception as exc:
            raise ApiError(502, f"更新確認に失敗: {exc}") from exc
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/mods/check-updates", mods_check_updates)

    def _ssh_reachable(profile, timeout=1.5) -> bool:
        import socket
        try:
            with socket.create_connection(
                    (profile.address, getattr(profile, "ssh_port", 22)), timeout):
                return True
        except Exception:
            return False

    def _with_vm_ssh(profile, fn, progress=lambda t: None):
        """SSHが要る操作。VMが停止中なら 起動→操作→停止 して元の状態に戻す。"""
        started = False
        vm = profile.vm
        if vm and not _ssh_reachable(profile):
            from core.orchestration import _wait_for_port
            import time as _t
            progress(f"VM {vm} が停止中のため起動します(数十秒)…")
            ctx.hyperv.start_vm(vm)
            _wait_for_port(profile.address, getattr(profile, "ssh_port", 22), 240)
            _t.sleep(4)
            started = True
        try:
            return fn()
        finally:
            if started:
                progress(f"操作完了。VM {vm} を停止して元に戻します…")
                try:
                    ctx.hyperv.stop_vm(vm, force=False)
                except Exception:
                    pass

    def server_config_get(params, **_):
        """MCの server.properties を全キー読む(順序保持)。VM停止中は一時起動して読む。"""
        srv = _srv(params)
        if srv.profile.game != "minecraft":
            raise ApiError(400, "server.propertiesの編集はMinecraftのみ対応です")
        from core import serverconfig
        try:
            text = _with_vm_ssh(srv.profile,
                                lambda: serverconfig.read_config(srv.profile))
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

        def _write():
            text = serverconfig.read_config(srv.profile)
            props = serverconfig.Properties(text)
            for k, v in changes.items():
                props.set(k, str(v))
            serverconfig.write_config(srv.profile, props.text(), restart=restart,
                                      progress=jobs.progress)
            return f"{len(changes)}項目を保存"

        def fn():
            # VM停止中なら 起動→保存→停止(restart=Falseなら停止のまま戻る)
            return _with_vm_ssh(srv.profile, _write, progress=jobs.progress)
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
            res = _with_vm_ssh(srv.profile, lambda: palupdate.check(srv.profile))
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
        t = jobs.submit(
            f"⬆ 更新: {srv.profile.display_name}",
            lambda: _with_vm_ssh(
                srv.profile,
                lambda: palupdate.update(srv.profile, progress=jobs.progress),
                jobs.progress),
            lane=server_lane(params["name"]), category="更新")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/update", server_update)

    # ---- MC 既存ワールドのバージョン変更(アップグレードのみ) ----
    def mc_versions(params, **_):
        srv = _srv(params)
        if srv.profile.game != "minecraft":
            raise ApiError(400, "バージョン変更はMinecraftのみ対応です")
        from core import mcversion
        try:
            cur = _with_vm_ssh(srv.profile,
                               lambda: mcversion.installed_version(srv.profile))
            choices = mcversion.upgradable_versions(cur) if cur else []
        except Exception as exc:
            raise ApiError(502, f"バージョン取得に失敗: {exc}") from exc
        return {"current": cur, "choices": choices}
    r.add("GET", r"/api/servers/(?P<name>[^/]+)/mc-versions", mc_versions)

    def mc_version_plan(params, body, **_):
        srv = _srv(params)
        if srv.profile.game != "minecraft":
            raise ApiError(400, "Minecraftのみ対応です")
        target = str((body or {}).get("target", "")).strip()
        if not target:
            raise ApiError(400, "target(目標バージョン)を指定してください")
        from core import mcversion

        def _plan():
            cur = mcversion.installed_version(srv.profile)
            if not mcversion.is_upgrade(cur, target):
                raise ApiError(400,
                               f"ダウングレード/同一版は不可です(現在 {cur} → {target})")
            return cur, mcversion.mod_plan(srv.profile, target)
        try:
            cur, plan = _with_vm_ssh(srv.profile, _plan)   # 停止中は起動→確認→停止
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(502, f"mod互換確認に失敗: {exc}") from exc
        return {
            "current": cur, "target": target, "mods": plan,
            "updatable": [m for m in plan if m["status"] == "update"],
            "incompatible": [m for m in plan if m["status"] == "incompatible"],
            "unknown": [m for m in plan if m["status"] == "unknown"],
        }
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/mc-version-plan", mc_version_plan)

    def mc_version_change(params, body, **_):
        srv = _srv(params)
        if srv.profile.game != "minecraft":
            raise ApiError(400, "Minecraftのみ対応です")
        target = str((body or {}).get("target", "")).strip()
        from core import mcversion
        mark("restart", f"mc:{params['name']}")      # 更新中の停止=意図的
        prof, bcfg = srv.profile, ctx.backupcfg

        def job():
            # VM停止中でも実施(起動→変更→停止)。ダウングレード判定もここで(1サイクル)
            cur = mcversion.installed_version(prof)
            if not mcversion.is_upgrade(cur, target):
                raise RuntimeError(f"ダウングレードはできません(現在 {cur} → {target})")
            plan = mcversion.mod_plan(prof, target, progress=jobs.progress)
            return mcversion.change_version(prof, target, plan, bcfg,
                                            progress=jobs.progress)
        t = jobs.submit(f"⬆ バージョン変更 →{target}: {prof.display_name}",
                        lambda: _with_vm_ssh(prof, job, jobs.progress),
                        lane=server_lane(params["name"]), category="バージョン変更")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/mc-version-change", mc_version_change)

    # ---------------- MC メモリ変更(JVMヒープ / VM RAM) ----------------
    _host_mem = {"mb": 0}

    def _host_total_mb() -> int:
        """ホストの物理メモリ(MB)を実測して返す(初回のみ取得、以後キャッシュ)。"""
        if not _host_mem["mb"]:
            try:
                r = ctx.runner.run_ps(
                    "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory")
                _host_mem["mb"] = int(int(r.stdout.strip()) / 1048576)
            except Exception:
                _host_mem["mb"] = 48 * 1024      # 取得失敗時のフォールバック
        return _host_mem["mb"]

    def _check_vm_cap(vm_max_mb: int, example: str) -> None:
        total = _host_total_mb()
        if vm_max_mb > total:
            raise ApiError(400, f"VMメモリ({vm_max_mb/1024:.0f}GB)がホストの物理メモリ"
                           f"({total/1024:.0f}GB)を超えています。GB単位で入力して"
                           f"ください(例: {example})")

    def mc_memory_get(params, **_):
        srv = _srv(params)
        game = srv.profile.game
        if game not in ("minecraft", "palworld"):
            raise ApiError(400, "メモリ変更はMinecraft/Palworldのみ対応です")
        from core import mcmemory
        try:
            if game == "palworld":            # ネイティブ=ヒープ無し、VMメモリのみ
                return mcmemory.read_vm(srv.profile, ctx.hyperv)
            return mcmemory.read(srv.profile, ctx.hyperv)
        except Exception as exc:
            raise ApiError(502, f"メモリ情報の取得に失敗: {exc}") from exc
    r.add("GET", r"/api/servers/(?P<name>[^/]+)/mc-memory", mc_memory_get)

    def mc_memory_set(params, body, **_):
        srv = _srv(params)
        game = srv.profile.game
        if game not in ("minecraft", "palworld"):
            raise ApiError(400, "メモリ変更はMinecraft/Palworldのみ対応です")
        b = body or {}
        if game == "palworld":
            # Palworld=VMメモリのみ。ライブ変更(動的+稼働中)ならサービス無停止。
            try:
                vm_max_mb = int(round(float(b.get("vm_gb")) * 1024))
            except (TypeError, ValueError):
                raise ApiError(400, "vm_gb(VMメモリGB)を数値で指定してください")
            if vm_max_mb < 2048:
                raise ApiError(400, "PalworldのVMメモリは2GB以上にしてください")
            _check_vm_cap(vm_max_mb, "12 = 12GB")
            dynamic = bool(b.get("dynamic", True))
            from core import mcmemory
            prof = srv.profile
            mark("restart", f"mc:{params['name']}")

            def pal_job():
                return mcmemory.change_vm_only(prof, ctx.hyperv, vm_max_mb,
                                               dynamic=dynamic,
                                               progress=jobs.progress)
            t = jobs.submit(f"🧠 メモリ変更: {prof.display_name}", pal_job,
                            lane=server_lane(params["name"]), category="メモリ")
            return {"task_id": t.id}
        try:
            heap_mb = int(round(float(b.get("heap_gb", 0)) * 1024))
        except (TypeError, ValueError):
            raise ApiError(400, "heap_gb(ヒープGB)を数値で指定してください")
        if heap_mb < 512:
            raise ApiError(400, "ヒープは0.5GB以上にしてください")
        dynamic = bool(b.get("dynamic", True))
        vm_gb = b.get("vm_gb")
        vm_max_mb = None
        if vm_gb not in (None, ""):
            vm_max_mb = int(round(float(vm_gb) * 1024))
            need = heap_mb + (1024 if dynamic else 512)
            if vm_max_mb < need:
                raise ApiError(400, f"VMメモリ({vm_max_mb}MB)がヒープ+余裕({need}MB)より"
                               "小さいです。VMメモリを増やすかヒープを下げてください")
            _check_vm_cap(vm_max_mb, "8 = 8GB")
        else:
            # ヒープのみ変更: 現在のVM RAMに収まるか検証
            from core import mcmemory as _mm
            try:
                cur = ctx.hyperv.get_memory(srv.profile.vm) if srv.profile.vm else {}
            except Exception:
                cur = {}
            avail = (cur.get("max_mb") if cur.get("dynamic") else cur.get("startup_mb")) or 0
            if avail and heap_mb + 512 > avail:
                raise ApiError(400, f"ヒープ({heap_mb}MB)がVMのRAM({avail}MB)を超えます。"
                               "VMメモリも一緒に増やしてください")
        from core import mcmemory
        prof = srv.profile
        mark("restart", f"mc:{params['name']}")

        def job():
            return mcmemory.change(prof, ctx.hyperv, heap_mb, vm_max_mb=vm_max_mb,
                                   dynamic=dynamic, progress=jobs.progress)
        t = jobs.submit(f"🧠 メモリ変更: {prof.display_name}", job,
                        lane=server_lane(params["name"]), category="メモリ")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/mc-memory", mc_memory_set)

    # ---------------- MC クラスタ管理 ----------------
    def _cm():
        from core.mccluster import ClusterManager
        return ClusterManager(ctx.config, ctx.runner)

    def clusters_get(**_):
        return _cm().summary()
    r.add("GET", "/api/clusters", clusters_get)

    def cluster_create(body, **_):
        from core.mccluster import ClusterError
        try:
            return _cm().create(str((body or {}).get("name", "")))
        except ClusterError as exc:
            raise ApiError(400, str(exc)) from exc
    r.add("POST", "/api/clusters/create", cluster_create)

    def _cluster_job(title, fn):
        t = jobs.submit(title, fn, lane="mc-cluster", category="クラスタ")
        return {"task_id": t.id}

    def cluster_delete(params, **_):
        name = params["name"]
        return _cluster_job(f"🌐 クラスタ削除: {name}",
                            lambda: _cm().delete(name, progress=jobs.progress))
    r.add("POST", r"/api/clusters/(?P<name>[^/]+)/delete", cluster_delete)

    def cluster_add_member(params, body, **_):
        name = params["name"]
        server = str((body or {}).get("server", ""))
        share = bool((body or {}).get("share", False))
        return _cluster_job(
            f"🌐 {name} にサーバー追加: {server}"
            + (" (共有ON)" if share else " (共有OFF)"),
            lambda: _cm().add_member(name, server, share, progress=jobs.progress))
    r.add("POST", r"/api/clusters/(?P<name>[^/]+)/members", cluster_add_member)

    def cluster_set_share(params, body, **_):
        name, server = params["name"], params["server"]
        share = bool((body or {}).get("share", False))
        return _cluster_job(
            f"🌐 {name}/{server} 共有{'ON' if share else 'OFF'}",
            lambda: _cm().set_share(name, server, share, progress=jobs.progress))
    r.add("POST",
          r"/api/clusters/(?P<name>[^/]+)/members/(?P<server>[^/]+)/share",
          cluster_set_share)

    def cluster_remove_member(params, **_):
        name, server = params["name"], params["server"]
        return _cluster_job(
            f"🌐 {name} からサーバー除外: {server}",
            lambda: _cm().remove_member(name, server, progress=jobs.progress))
    r.add("POST",
          r"/api/clusters/(?P<name>[^/]+)/members/(?P<server>[^/]+)/remove",
          cluster_remove_member)

    # ---------------- サーバー削除(config・任意でVMごと) ----------------
    def server_delete(params, body, **_):
        srv = _srv(params)
        name = params["name"]
        prof = srv.profile
        del_vm = bool((body or {}).get("delete_vm", False))
        do_backup = bool((body or {}).get("backup", False))
        vm = prof.vm
        if del_vm and vm:
            others = [s.profile.display_name for s in ctx.servers.values()
                      if s.profile.vm == vm and s.profile.name != name]
            if others:
                raise ApiError(400, f"VM {vm} には他のサーバー({', '.join(others)})も"
                               "載っています。VMごと削除はできません(設定から削除のみ可)")
        mark("stop", f"mc:{name}")

        def job():
            p = jobs.progress
            try:
                p("サーバーを停止中…")
                srv.stop()
            except Exception as exc:
                p(f"停止に失敗(続行): {exc}")
            if do_backup:                     # 削除前バックアップ(VMはまだ生きているのでSSHで取れる)
                try:
                    p("削除前バックアップを取得中…")
                    bk = backup.pal_backup if prof.game == "palworld" else backup.mc_backup
                    bk(prof, ctx.backupcfg, progress=p)
                except Exception as exc:
                    p(f"バックアップに失敗(続行): {exc}")
            try:
                from core.mccluster import ClusterManager
                ClusterManager(ctx.config, ctx.runner).forget_server(
                    name, undeploy=not del_vm, progress=p)
            except Exception as exc:
                p(f"クラスタ掃除に失敗(続行): {exc}")
            try:
                from service import pubstat
                p("外部公開を停止中…")
                pubstat.unpublish_server(ctx, prof)
            except Exception as exc:
                p(f"外部公開停止に失敗(続行): {exc}")
            # DNSのA/PTR/SRVを掃除(孤児レコードを残さない)。設定とfqdnがある時のみ。
            if getattr(prof, "fqdn", None) and getattr(ctx.config, "dns", None) is not None:
                try:
                    from core import dnsreg
                    p(f"DNSレコードを削除中({prof.fqdn})…")
                    dnsreg.unregister_host(ctx.config.dns, prof.fqdn, prof.address,
                                           service="minecraft", progress=p)
                except Exception as exc:
                    p(f"DNS掃除に失敗(続行): {exc}")
            p("config.yamlから削除中…")
            from core import settings
            settings.remove_profile(ctx.config_path, name)
            vm_deleted = False
            if del_vm and vm:
                p(f"VM {vm} を削除中…")
                vm_deleted = ctx.hyperv.delete_vm(vm, delete_disks=True)
            ctx.reload()
            return {"deleted": name, "vm_deleted": vm_deleted}

        title = f"🗑 サーバー削除: {prof.display_name}" + (" (VMごと)" if del_vm else "")
        t = jobs.submit(title, job, lane=server_lane(name), category="削除")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/delete", server_delete)

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

    def vm_clone(body, **_):
        """テンプレVMを複製→個体化(hostname/IP)して、すぐ構築できる空VMを作る。

        body: {template, new_name, hostname, new_ip, template_ip, ssh_user,
               ssh_password, memory_gb?, cpu?}
        流れ: clone_vm → start_vm → SSH応答待ち(template_ip) → individualize_clone(reboot)。
        """
        from core import orchestration as orch
        b = body or {}
        template = b.get("template")
        new_name = (b.get("new_name") or "").strip()
        hostname = (b.get("hostname") or "").strip()
        new_ip_in = (b.get("new_ip") or "").strip()
        template_ip = (b.get("template_ip") or "").strip()
        ssh_user = b.get("ssh_user")
        ssh_pass = b.get("ssh_password")
        if not all([template, new_name, hostname, new_ip_in, template_ip,
                    ssh_user, ssh_pass]):
            raise ApiError(400, "template / new_name / hostname / new_ip / "
                           "template_ip / ssh_user / ssh_password は必須です")
        try:
            mem_mb = int(float(b.get("memory_gb") or 4) * 1024)
            cpu = int(b.get("cpu") or 4)
        except (TypeError, ValueError):
            raise ApiError(400, "メモリ/CPUは数値で指定してください")
        net = getattr(ctx.config, "network", None)
        new_ip = net.full_ip(new_ip_in) if net else new_ip_in
        gateway = net.gateway if net else "192.168.11.1"
        dns = ctx.config.dns.host if getattr(ctx.config, "dns", None) else "192.168.11.254"

        def fn():
            jobs.progress(f"{template} を {new_name} に複製中…")
            ctx.hyperv.clone_vm(template, new_name, mem_mb, cpu)
            jobs.progress(f"{new_name} を起動→SSH応答待ち({template_ip})…")
            ctx.hyperv.start_vm(new_name)
            orch._wait_for_port(template_ip, 22, timeout=180)
            jobs.progress(f"個体化(hostname={hostname} / IP={new_ip})…再起動します")
            orch.individualize_clone(template_ip, ssh_user, ssh_pass, hostname,
                                     new_ip, gateway, dns, progress=jobs.progress)
            return (f"VM {new_name}(IP {new_ip})を作成しました。"
                    "「⚙ 新規構築」でサーバーを入れられます。")
        t = jobs.submit(f"📋 VMクローン: {new_name}", fn, category="VM")
        return {"task_id": t.id}
    r.add("POST", "/api/vms/clone", vm_clone)

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

    def vm_delete(params, body, **_):
        name = params["name"]
        delete_disks = bool((body or {}).get("delete_disks", True))
        linked = [s.profile.display_name for s in ctx.servers.values()
                  if s.profile.vm == name]
        if linked:
            raise ApiError(400, f"VM {name} にはサーバー({', '.join(linked)})が"
                           "登録されています。先にサーバー削除を行ってください")

        def fn():
            jobs.progress(f"VM {name} を削除中…")
            existed = ctx.hyperv.delete_vm(name, delete_disks=delete_disks)
            return "deleted" if existed else "not_found"
        t = jobs.submit(f"🗑 VM削除: {name}" + (" (ディスクごと)" if delete_disks else ""),
                        fn, lane=f"vm:{name}", category="VM操作")
        return {"task_id": t.id}
    r.add("POST", r"/api/vms/(?P<name>[^/]+)/delete", vm_delete)

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

    # ---------------- ネットワーク(DNS登録状況 / ポート管理状況) ----------------
    def network_status(**_):
        """DNS登録状況とUPnPポート開放状況をまとめて返す(ネットワークタブ用)。"""
        from core import conntest
        resolver = ctx.config.dns.host if getattr(ctx.config, "dns", None) else None
        domain = ctx.config.dns.domain if resolver else None

        # ---- DNS: サーバーごとに .254 で解決して状態を出す ----
        dns_rows = []
        for name, srv in ctx.servers.items():
            p = srv.profile
            fqdn = getattr(p, "fqdn", None)
            row = {"name": name, "display": p.display_name, "game": p.game,
                   "fqdn": fqdn, "address": p.address, "a": [], "srv": None,
                   "resolves": False, "lan_match": None}
            if resolver and fqdn:
                try:
                    a = conntest.dns_query(resolver, fqdn, 1, timeout=3)
                    row["a"] = a
                    row["resolves"] = bool(a)
                    row["lan_match"] = p.address in a
                except Exception as exc:
                    row["error"] = str(exc)
                if p.game != "palworld":              # SRV(外部公開の名前ルーティング)
                    try:
                        s = conntest.dns_query(resolver, f"_minecraft._tcp.{fqdn}",
                                               33, timeout=3)
                        if s:
                            _pr, _w, sport, starget = s[0]
                            row["srv"] = {"port": sport, "target": starget}
                    except Exception:
                        pass
            dns_rows.append(row)

        # ---- ポート: UPnPマッピングを1回だけ取得して整理 ----
        ports = {"enabled": portsync.enabled if portsync else None,
                 "wan": None, "gateway_ok": False, "mappings": [], "servers": []}
        mappings = []
        try:
            from service import pubstat
            gw = pubstat._gateway(ctx)
            mappings = gw.client.list_port_mappings()
            ports["wan"] = gw.external_ip
            ports["gateway_ok"] = True
        except Exception as exc:
            ports["error"] = str(exc)

        def _owner(ep, proto, ic):
            for _n, s in ctx.servers.items():
                pp = s.profile
                pproto = "UDP" if pp.game == "palworld" else "TCP"
                if (str(getattr(pp, "external_port", None)) == ep and pproto == proto
                        and ic == pp.address):
                    return pp.display_name
            for ah in ctx.arkhosts:
                if ep in (str(getattr(ah.cfg, "game_port", None)),
                          str(getattr(ah.cfg, "query_port", None))):
                    return ah.cfg.display_name
            return None

        for m in mappings:
            ep = str(m.get("external_port"))
            proto = (m.get("protocol") or "").upper()
            desc = m.get("description") or ""
            ports["mappings"].append({
                "external_port": m.get("external_port"), "protocol": proto,
                "internal_client": m.get("internal_client"),
                "internal_port": m.get("internal_port"), "description": desc,
                "owner": _owner(ep, proto, m.get("internal_client")),
                "gsm": desc.startswith("gsm-"),
            })

        existing = {(str(m.get("external_port")), (m.get("protocol") or "").upper()): m
                    for m in mappings}
        for name, srv in ctx.servers.items():
            p = srv.profile
            ep = getattr(p, "external_port", None)
            proto = "UDP" if p.game == "palworld" else "TCP"
            m = existing.get((str(ep), proto)) if ep else None
            ports["servers"].append({
                "name": name, "display": p.display_name, "game": p.game,
                "external_port": ep, "proto": proto,
                "game_port": getattr(p, "game_port", None),
                "forwarded": bool(m and m.get("internal_client") == p.address),
            })
        return {"resolver": resolver, "domain": domain,
                "dns": dns_rows, "ports": ports}
    r.add("GET", "/api/network", network_status)

    def server_dns_register(params, **_):
        """サーバーのfqdn→LAN IPをDNSに(再)登録する(ネットワークタブの便利ボタン)。"""
        srv = _srv(params)
        p = srv.profile
        if getattr(ctx.config, "dns", None) is None:
            raise ApiError(400, "DNS設定(dns:)がありません")
        fqdn = getattr(p, "fqdn", None) or f"{params['name']}.{ctx.config.dns.domain}"

        def job():
            from core import dnsreg
            f = dnsreg.register_host(ctx.config.dns, fqdn, p.address,
                                     progress=jobs.progress)
            return f"DNS登録: {f} → {p.address}"
        t = jobs.submit(f"🌐 DNS登録: {p.display_name}", job, category="DNS")
        return {"task_id": t.id}
    r.add("POST", r"/api/servers/(?P<name>[^/]+)/dns-register", server_dns_register)

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
