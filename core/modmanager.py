"""汎用のMinecraft MOD管理(ローカルjarライブラリ + サーバーへの配布)。

- ホスト側の modlib/ に jar(ばら=個別追加用ライブラリ)と
  modlib/packs/<名前>/ (modpack=jar群) を置く。
- サーバーの mods/ に対して「個別追加/削除」「modpack一括適用(同期)」を行う。
- mods/ は runtime_user 所有なので /tmp にSFTP → sudo で mv+chown する(moddeployと同方式)。

SQL共有連動の自動デプロイ(InvSync)は moddeploy.py が担当。こちらは手動のmod管理用。
mods場所・所有者は GameServerProfile.mods_dir / runtime_user から取る。
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import paramiko

from . import onlinemods


class ModError(Exception):
    pass


# ---------------------------------------------------------------------------
# ホスト側 modライブラリ(modlib/)
# ---------------------------------------------------------------------------
def _safe_ver(version: str) -> str:
    """バージョン文字列をフォルダ名として安全化する。"""
    v = (version or "").strip()
    for ch in '/\\:*?"<>|':
        v = v.replace(ch, "_")
    return v


@dataclass
class ModLibrary:
    """Minecraftバージョンごとに分けたローカルmodライブラリ。

    レイアウト:
      modlib/<version>/*.jar          … ばらmod(個別追加用)
      modlib/<version>/packs/<名前>/  … modpack(jar群)
    """
    base_dir: Path            # GSMプロジェクトルート
    dirname: str = "modlib"

    @property
    def root(self) -> Path:
        p = Path(self.dirname)
        return p if p.is_absolute() else Path(self.base_dir) / self.dirname

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def version_dir(self, version: str) -> Path:
        return self.root / _safe_ver(version)

    def ensure_version(self, version: str) -> None:
        if not version.strip():
            return
        (self.version_dir(version) / "packs").mkdir(parents=True, exist_ok=True)

    def versions(self) -> list[str]:
        """modlib/ 直下にあるバージョンフォルダ一覧。"""
        if not self.root.exists():
            return []
        return sorted(d.name for d in self.root.iterdir() if d.is_dir())

    def loose_mods(self, version: str) -> list[str]:
        """modlib/<version>/ 直下のjar(個別追加できるライブラリ)。"""
        d = self.version_dir(version)
        if not d.exists():
            return []
        return sorted(f.name for f in d.iterdir()
                      if f.is_file() and f.suffix.lower() == ".jar")

    def loose_path(self, version: str, name: str) -> Path:
        return self.version_dir(version) / name

    def packs_dir(self, version: str) -> Path:
        return self.version_dir(version) / "packs"

    def packs(self, version: str) -> list[str]:
        p = self.packs_dir(version)
        if not p.exists():
            return []
        return sorted(d.name for d in p.iterdir() if d.is_dir())

    def pack_jars(self, version: str, pack: str) -> list[Path]:
        d = self.packs_dir(version) / pack
        if not d.exists():
            raise ModError(f"modpackが見つかりません: {version}/{pack}")
        return sorted(f for f in d.iterdir()
                      if f.is_file() and f.suffix.lower() == ".jar")


# ---------------------------------------------------------------------------
# サーバー側操作(SSH/SFTP)
# ---------------------------------------------------------------------------
def _connect(profile) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(profile.address, username=profile.ssh_user,
                  password=profile.ssh_password, port=profile.ssh_port, timeout=15)
    except Exception as exc:
        raise ModError(f"{profile.address} にSSH接続できません: {exc}") from exc
    return c


def _sudo_run(client: paramiko.SSHClient, password: str, script: str) -> str:
    sftp = client.open_sftp()
    with sftp.open("/tmp/gsm_modmanager.sh", "w") as f:
        f.write(script)
    sftp.close()
    stdin, stdout, _ = client.exec_command(
        "sudo -S bash /tmp/gsm_modmanager.sh 2>&1", timeout=180)
    stdin.write(password + "\n")
    stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    if "MODMGR_OK" not in out:
        raise ModError(f"MOD操作に失敗しました:\n{out[-800:]}")
    return out


def _restart_line(profile, restart: bool) -> str:
    if not restart or not profile.service:
        return "true"
    return f"systemctl restart {profile.service}"


def list_installed(profile) -> list[str]:
    """サーバーの mods/ にある jar 名の一覧。"""
    client = _connect(profile)
    try:
        cmd = (f"ls -1 '{profile.mods_dir}' 2>/dev/null "
               f"| grep -iE '\\.jar$' || true")
        _, stdout, _ = client.exec_command(cmd, timeout=20)
        out = stdout.read().decode("utf-8", "replace")
        return sorted(line.strip() for line in out.splitlines() if line.strip())
    finally:
        client.close()


def add_mods(profile, jar_paths, restart: bool = True,
             progress=lambda t: None) -> None:
    """ローカルの jar 群をサーバーの mods/ に追加する。"""
    paths = [Path(p) for p in jar_paths]
    for p in paths:
        if not p.exists():
            raise ModError(f"{p} がありません")
    if not paths:
        return
    client = _connect(profile)
    try:
        progress(f"{profile.display_name}: {len(paths)}個のMODを転送中…")
        sftp = client.open_sftp()
        for p in paths:
            sftp.put(str(p), f"/tmp/{p.name}")
        sftp.close()
        moves = "\n".join(
            f"mv -f '/tmp/{p.name}' '{profile.mods_dir}/{p.name}'" for p in paths)
        script = f"""#!/bin/bash
set -e
mkdir -p '{profile.mods_dir}'
{moves}
chown -R {profile.runtime_user}:{profile.runtime_user} '{profile.mods_dir}'
{_restart_line(profile, restart)}
echo MODMGR_OK
"""
        progress(f"{profile.display_name}: 配置中"
                 f"{'・再起動' if restart and profile.service else ''}…")
        _sudo_run(client, profile.ssh_password, script)
    finally:
        client.close()


def remove_mods(profile, jar_names, restart: bool = True,
                progress=lambda t: None) -> None:
    """サーバーの mods/ から jar を削除する。"""
    names = [n for n in jar_names if n]
    if not names:
        return
    client = _connect(profile)
    try:
        rms = "\n".join(f"rm -f '{profile.mods_dir}/{name}'" for name in names)
        script = f"""#!/bin/bash
set -e
{rms}
{_restart_line(profile, restart)}
echo MODMGR_OK
"""
        progress(f"{profile.display_name}: {len(names)}個のMODを削除中…")
        _sudo_run(client, profile.ssh_password, script)
    finally:
        client.close()


_META_SCRIPT = r"""
import os, sys, zipfile, json
d = sys.argv[1]
try:
    names = sorted(f for f in os.listdir(d) if f.lower().endswith('.jar'))
except Exception:
    names = []
for fn in names:
    info = {'file': fn, 'id': None, 'name': fn, 'version': '?'}
    try:
        z = zipfile.ZipFile(os.path.join(d, fn))
        j = json.loads(z.read('fabric.mod.json').decode('utf-8', 'replace'))
        info['id'] = j.get('id')
        info['name'] = j.get('name') or fn
        info['version'] = str(j.get('version') or '?')
    except Exception:
        pass
    print(json.dumps(info))
"""


def list_installed_meta(profile) -> list[dict]:
    """サーバーの mods/ の各jarから fabric.mod.json を読み、
    [{file, id, name, version}] を返す(VM上のpython3で抽出。sudo不要)。"""
    client = _connect(profile)
    try:
        sftp = client.open_sftp()
        with sftp.open("/tmp/gsm_modmeta.py", "w") as f:
            f.write(_META_SCRIPT)
        sftp.close()
        _, stdout, _ = client.exec_command(
            f"python3 /tmp/gsm_modmeta.py '{profile.mods_dir}'", timeout=60)
        out = stdout.read().decode("utf-8", "replace")
    finally:
        client.close()
    mods = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                mods.append(json.loads(line))
            except ValueError:
                pass
    return mods


def export_mods_zip(profile, dest_zip: str | Path,
                    progress=lambda t: None) -> int:
    """サーバーの mods/ の全jarをローカルにDLして dest_zip に1つのZIPへまとめる。

    クライアント(自分/友達)の .minecraft/mods にそのまま入れて使う用。導入したjar数を返す。
    """
    import tempfile
    import zipfile

    client = _connect(profile)
    try:
        sftp = client.open_sftp()
        names = sorted(f for f in sftp.listdir(profile.mods_dir)
                       if f.lower().endswith(".jar"))
        if not names:
            raise ModError("mods フォルダに jar がありません")
        tmp = Path(tempfile.mkdtemp(prefix="gsm_modszip_"))
        locals_ = []
        for i, fn in enumerate(names, 1):
            progress(f"取得中 ({i}/{len(names)}): {fn}")
            lp = tmp / fn
            sftp.get(f"{profile.mods_dir}/{fn}", str(lp))
            locals_.append(lp)
        sftp.close()
    finally:
        client.close()
    progress("ZIPにまとめ中…")
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for lp in locals_:
            z.write(lp, lp.name)
    progress(f"完了: {len(locals_)}個 → {dest_zip}")
    return len(locals_)


def install_online(profile, entries: list[dict], restart: bool = True,
                   progress=lambda t: None) -> list[str]:
    """onlinemods.collect_with_deps の結果(entryのリスト)をDLしてサーバーに導入する。"""
    entries = [e for e in entries if isinstance(e, dict) and e.get("url")]
    if not entries:
        raise ModError("導入するファイルがありません")
    tmp = Path(tempfile.mkdtemp(prefix="gsm_mods_"))
    paths = []
    for e in entries:
        dest = tmp / e["filename"]
        progress(f"ダウンロード中: {e['name']} {e.get('version','')}")
        onlinemods.download(e["url"], str(dest))
        paths.append(str(dest))
    add_mods(profile, paths, restart=restart, progress=progress)
    return [e["filename"] for e in entries]


def check_updates_modrinth(profile, mcver: str,
                           progress=lambda t: None) -> list[dict]:
    """導入済みjarのSHA1をModrinthに照会し、更新有無を判定する。

    戻り値: [{file, name, current, latest, update, source}] 。
      source='modrinth'=判定できた / 'unknown'=Modrinth未登録(CF等)で判定不可。
    """
    import hashlib
    import urllib.error
    import urllib.request

    client = _connect(profile)
    try:
        _, stdout, _ = client.exec_command(
            f"sha1sum '{profile.mods_dir}'/*.jar 2>/dev/null", timeout=60)
        raw = stdout.read().decode("utf-8", "replace")
    finally:
        client.close()
    hashes = {}                                   # sha1 -> filename
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            hashes[parts[0]] = Path(parts[-1]).name

    out = []
    for sha1, fname in hashes.items():
        rec = {"file": fname, "name": fname, "current": "?",
               "latest": None, "update": False, "source": "unknown"}
        try:
            req = urllib.request.Request(
                f"https://api.modrinth.com/v2/version_file/{sha1}?algorithm=sha1",
                headers={"User-Agent": "game-server-manager"})
            with urllib.request.urlopen(req, timeout=20) as r:
                ver = json.load(r)
            rec["source"] = "modrinth"
            rec["current"] = ver.get("version_number", "?")
            pid = ver.get("project_id")
            latest = onlinemods.resolve_modrinth(pid, mcver)  # 最新
            rec["name"] = latest.get("name", fname)
            rec["latest"] = latest.get("version")
            rec["update"] = bool(rec["latest"] and rec["latest"] != rec["current"])
        except (urllib.error.HTTPError, onlinemods.ModSearchError):
            pass                                  # Modrinth未登録 or 対応版なし
        except Exception:
            pass
        out.append(rec)
        progress(f"更新確認: {rec['name']}")
    return out


def apply_pack(profile, library: ModLibrary, version: str, pack: str,
               prune: bool = False, restart: bool = True,
               progress=lambda t: None) -> dict:
    """modpack(version配下)の内容をサーバーの mods/ に反映する。

    prune=True なら pack に無い jar を削除して完全同期する。
    戻り値: {"added": [...アップロードしたjar...], "removed": [...削除したjar...]}
    """
    jars = library.pack_jars(version, pack)
    if not jars:
        raise ModError(f"modpack '{pack}' に jar がありません")
    pack_names = {p.name for p in jars}
    to_remove = []
    if prune:
        installed = set(list_installed(profile))
        to_remove = sorted(installed - pack_names)

    client = _connect(profile)
    try:
        progress(f"{profile.display_name}: modpack '{pack}' を転送中"
                 f"({len(jars)}個)…")
        sftp = client.open_sftp()
        for p in jars:
            sftp.put(str(p), f"/tmp/{p.name}")
        sftp.close()
        moves = "\n".join(
            f"mv -f '/tmp/{p.name}' '{profile.mods_dir}/{p.name}'" for p in jars)
        rms = "\n".join(f"rm -f '{profile.mods_dir}/{n}'" for n in to_remove)
        script = f"""#!/bin/bash
set -e
mkdir -p '{profile.mods_dir}'
{moves}
{rms}
chown -R {profile.runtime_user}:{profile.runtime_user} '{profile.mods_dir}'
{_restart_line(profile, restart)}
echo MODMGR_OK
"""
        progress(f"{profile.display_name}: 反映中"
                 f"{'・再起動' if restart and profile.service else ''}…")
        _sudo_run(client, profile.ssh_password, script)
    finally:
        client.close()
    return {"added": sorted(pack_names), "removed": to_remove}
