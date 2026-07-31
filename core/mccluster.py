"""Minecraft クラスタ管理。

1つのVelocityプロキシの下に「クラスタ」(=MCサーバー群)を複数定義できる。
クラスタ内のメンバーは /s で相互移動でき、メンバーごとに「アイテム共有 ON/OFF」を持つ。

  - 全メンバー: ClusterConnect + CombatSwitch + fabric-api を配置し online-mode=false、
    Velocity の [servers] に登録、combatswitch.json でクラスタ内 /s 割当。
  - share=ON メンバー: さらに InvSync + クラスタ専用の共有DB(SqlShareManager)を配置。
    → インベントリ/ステータス/エンダーチェスト共有。
  - share=OFF メンバー: InvSync なし = 独立インベントリ。/s 移動はできる。

状態は clusters.json:
  {"clusters": {"<name>": {"members": {"<server>": {"share": bool}}}}}
共有DBは 1クラスタ=1グループ(SqlShareManager "cl_<name>")。
Velocity再起動はホストのPowerShell(runner)経由。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core import modmanager
from core.paths import app_dir
from core.sqlshare import SqlShareManager

VELOCITY_DIR = Path(r"C:\Velocity")
JAVA_EXE = r"C:\Users\master\.jdks\temurin-25.0.3\bin\java.exe"
# 全メンバー共通(fabric-api は ClusterConnect/CombatSwitch の依存なので常時必要)
BASE_JARS = ["fabric-api.jar", "clusterconnect-fabric.jar", "combatswitch.jar"]
SHARE_JAR = "invsyncmod.jar"
COMBAT_LOCK_SECONDS = 3.0


class ClusterError(Exception):
    pass


def _grp(name: str) -> str:
    return f"cl_{name}"


class ClusterManager:
    def __init__(self, config, runner, base_dir=None, config_path=None):
        self.config = config
        self.runner = runner
        self.base = Path(base_dir or app_dir())
        # proxied の書き戻しに使う。未指定なら base 直下の config.yaml
        self.config_path = Path(config_path) if config_path else self.base / "config.yaml"
        self.state_path = self.base / "clusters.json"
        self.modcache = self.base / "modcache"

    # ---------------- 参照 ----------------
    def _profiles(self) -> dict:
        return {p.name: p for p in self.config.servers if p.game == "minecraft"}

    def _profile(self, name):
        p = self._profiles().get(name)
        if not p:
            raise ClusterError(f"MCサーバー『{name}』が見つかりません")
        return p

    def _sql(self) -> SqlShareManager:
        if not getattr(self.config, "mysql", None):
            raise ClusterError("共有DB(mysql)が未設定です。設定タブでMySQLを設定してください")
        return SqlShareManager(self.config.mysql)

    def _secret(self) -> str:
        p = VELOCITY_DIR / "forwarding.secret"
        if not p.exists():
            raise ClusterError(f"{p} がありません(Velocityが未構築です)")
        return p.read_text(encoding="utf-8").strip()

    @staticmethod
    def _vname(profile) -> str:
        return profile.name          # Velocity上のサーバー名 = プロファイル名

    # ---------------- 状態 ----------------
    def load(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            d.setdefault("clusters", {})
            return d
        except (OSError, ValueError):
            return {"clusters": {}}

    def save(self, data) -> None:
        self.state_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _set_proxied(self, server: str, proxied: bool,
                     progress=lambda t: None) -> None:
        """クラスタ加入/脱退に合わせて config の proxied を切り替える。

        プロキシ配下のサーバーは online-mode=false で動かすため、直接公開すると
        誰でもなりすませる。よって加入時は公開対象から外し(proxied=true)、
        脱退時は元に戻す。手で付け外しすると必ず片方を忘れるので連動させる。
        """
        from . import settings
        try:
            settings.update_config(
                self.config_path, {"servers": {server: {"proxied": bool(proxied)}}})
            progress(f"{server}: 直接公開を{'停止' if proxied else '再開可能に'}しました")
        except Exception as exc:                  # noqa: BLE001 本処理は止めない
            progress(f"⚠ {server} の proxied 更新に失敗: {exc}")

    def _proxy_of(self, cluster_name: str) -> dict:
        """このクラスタを前段で受けるプロキシの接続情報。無ければ未設定を返す。"""
        from . import proxyreg
        for px in proxyreg.summary(self.config):
            if px.get("cluster") == cluster_name:
                return {"connect": px["connect"], "proxy": px["name"],
                        "proxy_port": px["port"]}
        return {"connect": "", "proxy": "", "proxy_port": 0}

    def summary(self) -> dict:
        data = self.load()
        profs = self._profiles()
        clusters = []
        assigned = set()
        for cname, c in data["clusters"].items():
            mem = []
            for sname, m in c.get("members", {}).items():
                assigned.add(sname)
                p = profs.get(sname)
                mem.append({
                    "server": sname,
                    "display": p.display_name if p else sname,
                    "share": bool(m.get("share")),
                    "address": p.address if p else "?",
                })
            # そのクラスタの前段プロキシ=プレイヤーに教える接続先。
            # 配下サーバーは直接繋げない(proxied)ので、アドレスはここにしか出せない。
            clusters.append({"name": cname, "members": mem,
                             **self._proxy_of(cname)})
        available = [{"server": n, "display": p.display_name}
                     for n, p in profs.items() if n not in assigned]
        return {"clusters": clusters, "available": available,
                "velocity_ok": (VELOCITY_DIR / "velocity.toml").exists()}

    # ---------------- クラスタCRUD ----------------
    def create(self, name):
        name = (name or "").strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ClusterError("クラスタ名は英数字・ハイフン・アンダースコアで指定してください")
        data = self.load()
        if name in data["clusters"]:
            raise ClusterError(f"クラスタ『{name}』は既に存在します")
        data["clusters"][name] = {"members": {}}
        self.save(data)
        return self.summary()

    def delete(self, name, progress=lambda t: None):
        data = self.load()
        c = data["clusters"].get(name)
        if c is None:
            raise ClusterError(f"クラスタ『{name}』がありません")
        for sname in list(c.get("members", {})):
            self.remove_member(name, sname, progress=progress, _defer_velocity=True)
        try:
            self._sql().delete_group(_grp(name))
        except Exception:
            pass
        data = self.load()
        data["clusters"].pop(name, None)
        self.save(data)
        self._rebuild_velocity(progress)
        return self.summary()

    # ---------------- メンバー操作 ----------------
    def add_member(self, cluster, server, share, progress=lambda t: None):
        data = self.load()
        if cluster not in data["clusters"]:
            raise ClusterError(f"クラスタ『{cluster}』がありません")
        for cn, c in data["clusters"].items():
            if server in c.get("members", {}):
                raise ClusterError(f"『{server}』は既にクラスタ『{cn}』に属しています")
        prof = self._profile(server)
        data["clusters"][cluster]["members"][server] = {"share": bool(share)}
        self.save(data)
        self._set_proxied(server, True, progress)   # プロキシ配下=直接公開しない
        if share:
            self._ensure_share_member(cluster, prof)
        self._deploy_cluster(cluster, progress)      # 全員のcombatswitchを揃える
        self._rebuild_velocity(progress)
        return self.summary()

    def set_share(self, cluster, server, share, progress=lambda t: None):
        data = self.load()
        m = data["clusters"].get(cluster, {}).get("members", {}).get(server)
        if m is None:
            raise ClusterError(f"『{server}』はクラスタ『{cluster}』にいません")
        prof = self._profile(server)
        share = bool(share)
        m["share"] = share
        self.save(data)
        if share:
            self._ensure_share_member(cluster, prof)
        else:
            try:
                self._sql().remove_server(_grp(cluster), prof.address)
            except Exception:
                pass
        self._deploy_server(cluster, server, progress)
        return self.summary()

    def remove_member(self, cluster, server, progress=lambda t: None,
                      _defer_velocity=False):
        data = self.load()
        c = data["clusters"].get(cluster, {})
        if server not in c.get("members", {}):
            raise ClusterError(f"『{server}』はクラスタ『{cluster}』にいません")
        prof = self._profile(server)
        if c["members"][server].get("share"):
            try:
                self._sql().remove_server(_grp(cluster), prof.address)
            except Exception:
                pass
        self._undeploy_server(prof, progress)
        del c["members"][server]
        self.save(data)
        self._set_proxied(server, False, progress)   # 単独運用に戻すので公開可に
        self._deploy_cluster(cluster, progress)      # 残メンバーの/s再割当
        if not _defer_velocity:
            self._rebuild_velocity(progress)
        return self.summary()

    def forget_server(self, server, undeploy=False, progress=lambda t: None):
        """サーバー削除時用: 全クラスタから外して掃除する。

        undeploy=True(VMを残す時): 可能ならSSHでmod除去+online-mode戻しを試みる。
        undeploy=False(VMごと削除時): SSHせず clusters.json/共有DB/Velocity だけ掃除。
        """
        data = self.load()
        profs = self._profiles()
        changed = False
        for cname, c in list(data["clusters"].items()):
            if server in c.get("members", {}):
                prof = profs.get(server)
                if c["members"][server].get("share") and prof:
                    try:
                        self._sql().remove_server(_grp(cname), prof.address)
                    except Exception:
                        pass
                if undeploy and prof:
                    try:
                        self._undeploy_server(prof, progress)
                    except Exception:
                        pass
                del c["members"][server]
                changed = True
        if changed:
            self.save(data)
            try:
                self._rebuild_velocity(progress)
            except Exception:
                pass
        return changed

    # ---------------- 共有DB ----------------
    def _ensure_share_member(self, cluster, prof):
        sm = self._sql()
        grp = _grp(cluster)
        if grp not in [g.name for g in sm.list_groups()]:
            sm.create_group(grp)
        sm.add_server(grp, prof.address)

    def _share_conn(self, cluster) -> dict:
        return self._sql().connection_info(_grp(cluster))

    @staticmethod
    def _invsync_props(prof, conn) -> str:
        return (
            f"server.name={prof.name}\n"
            f"db.host={conn['host']}\n"
            f"db.port={conn['port']}\n"
            f"db.name={conn['database']}\n"
            f"db.user={conn['user']}\n"
            f"db.password={conn['password']}\n"
            f"db.pool.max=10\n"
            f"db.pool.timeout=30000\n"
        )

    # ---------------- デプロイ ----------------
    def _deploy_cluster(self, cluster, progress):
        data = self.load()
        for server in list(data["clusters"][cluster]["members"].keys()):
            self._deploy_server(cluster, server, progress)

    def _deploy_server(self, cluster, server, progress):
        data = self.load()
        members = list(data["clusters"][cluster]["members"].keys())
        share = bool(data["clusters"][cluster]["members"][server].get("share"))
        prof = self._profile(server)
        profs = self._profiles()
        cs_servers = {f"s{i+1}": self._vname(profs[mem])
                      for i, mem in enumerate(members) if mem in profs}
        jars = list(BASE_JARS) + ([SHARE_JAR] if share else [])
        for j in jars:
            if not (self.modcache / j).exists():
                raise ClusterError(f"{self.modcache / j} がありません(modcacheにjarを置いてください)")
        conn = self._share_conn(cluster) if share else None
        progress(f"{prof.display_name}: クラスタ設定を配布中…")
        self._ssh_deploy(prof, jars, cs_servers, share, conn)

    def _ssh_deploy(self, prof, jars, cs_servers, share, conn):
        client = modmanager._connect(prof)
        mods = prof.mods_dir
        confd = f"{prof.install_dir}/config"
        prop = f"{prof.install_dir}/server.properties"
        try:
            sftp = client.open_sftp()
            for j in jars:
                sftp.put(str(self.modcache / j), f"/tmp/{j}")
            self._put_text(sftp, "/tmp/clusterconnect.json",
                           json.dumps({"secret_key": self._secret()}, indent=2))
            self._put_text(sftp, "/tmp/combatswitch.json",
                           json.dumps({"combat_lock_seconds": COMBAT_LOCK_SECONDS,
                                       "servers": cs_servers}, indent=2))
            if share and conn:
                self._put_text(sftp, "/tmp/invsyncmod.properties",
                               self._invsync_props(prof, conn))
            sftp.close()

            moves = "\n".join(f"mv -f /tmp/{j} '{mods}/{j}'" for j in jars)
            if share:
                inv_line = f"mv -f /tmp/invsyncmod.properties '{confd}/invsyncmod.properties'"
            else:
                inv_line = (f"rm -f '{mods}/{SHARE_JAR}' "
                            f"'{confd}/invsyncmod.properties'")
            script = f"""set -e
mkdir -p '{mods}' '{confd}'
{moves}
mv -f /tmp/clusterconnect.json '{confd}/clusterconnect.json'
mv -f /tmp/combatswitch.json '{confd}/combatswitch.json'
{inv_line}
if grep -q '^online-mode=' '{prop}'; then sed -i 's/^online-mode=.*/online-mode=false/' '{prop}'; else echo 'online-mode=false' >> '{prop}'; fi
chown -R {prof.runtime_user}:{prof.runtime_user} '{mods}' '{confd}' '{prop}'
systemctl restart '{prof.service}'
echo CLUSTER_DEPLOY_OK
"""
            out = self._sudo(client, prof.ssh_password, script)
            if "CLUSTER_DEPLOY_OK" not in out:
                raise ClusterError(f"{prof.display_name} の配布に失敗:\n{out[-600:]}")
        finally:
            client.close()

    def _undeploy_server(self, prof, progress):
        progress(f"{prof.display_name}: クラスタ設定を除去中…")
        client = modmanager._connect(prof)
        mods = prof.mods_dir
        confd = f"{prof.install_dir}/config"
        prop = f"{prof.install_dir}/server.properties"
        try:
            script = f"""set -e
rm -f '{mods}/clusterconnect-fabric.jar' '{mods}/combatswitch.jar' '{mods}/{SHARE_JAR}'
rm -f '{confd}/clusterconnect.json' '{confd}/combatswitch.json' '{confd}/invsyncmod.properties'
if grep -q '^online-mode=' '{prop}'; then sed -i 's/^online-mode=.*/online-mode=true/' '{prop}'; fi
systemctl restart '{prof.service}'
echo CLUSTER_UNDEPLOY_OK
"""
            out = self._sudo(client, prof.ssh_password, script)
            if "CLUSTER_UNDEPLOY_OK" not in out:
                raise ClusterError(f"{prof.display_name} の除去に失敗:\n{out[-600:]}")
        finally:
            client.close()

    # ---------------- Velocity ----------------
    def _rebuild_velocity(self, progress=lambda t: None):
        toml = VELOCITY_DIR / "velocity.toml"
        if not toml.exists():
            return
        data = self.load()
        profs = self._profiles()
        entries, first = [], None
        for c in data["clusters"].values():
            for sname in c.get("members", {}):
                p = profs.get(sname)
                if not p:
                    continue
                vn = self._vname(p)
                port = getattr(p, "game_port", None) or 25565
                entries.append(f'{vn} = "{p.address}:{port}"')
                if first is None:
                    first = vn
        if not entries:
            # メンバーが1つも無い状態でvelocity.tomlを空にするとプロキシが壊れるので触らない
            return
        block = "[servers]\n" + "\n".join(entries) + "\n\n"
        block += (f'try = [\n    "{first}"\n]\n' if first else "try = []\n")
        text = toml.read_text(encoding="utf-8")
        if "[forced-hosts]" in text:
            new = re.sub(r"\[servers\].*?(?=\[forced-hosts\])",
                         block + "\n", text, count=1, flags=re.S)
        else:
            new = re.sub(r"\[servers\].*?$", block, text, count=1, flags=re.S)
        toml.write_text(new, encoding="utf-8")
        progress("Velocity設定を更新して再起動中…")
        self._restart_velocity()

    def _restart_velocity(self):
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
            "Where-Object { $_.CommandLine -like '*velocity.jar*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }\n"
            "Start-Sleep -Seconds 1\n"
            f"Start-Process -FilePath '{JAVA_EXE}' "
            "-ArgumentList '-Xms512M','-Xmx1G','-XX:+UseG1GC','-jar','velocity.jar' "
            f"-WorkingDirectory '{VELOCITY_DIR}' -WindowStyle Hidden\n"
        )
        try:
            self.runner.run_ps(ps, timeout=30)
        except Exception:
            pass                     # Velocity再起動失敗はメンバー配布を止めない

    # ---------------- SSH小物 ----------------
    @staticmethod
    def _put_text(sftp, path, text):
        with sftp.open(path, "w") as f:
            f.write(text)

    @staticmethod
    def _sudo(client, password, script, timeout=180) -> str:
        sftp = client.open_sftp()
        with sftp.open("/tmp/gsm_cluster.sh", "w") as f:
            f.write(script)
        sftp.close()
        stdin, stdout, _ = client.exec_command(
            "sudo -S -p '' bash /tmp/gsm_cluster.sh 2>&1", timeout=timeout)
        stdin.write(password + "\n")
        stdin.flush()
        return stdout.read().decode("utf-8", "replace")
