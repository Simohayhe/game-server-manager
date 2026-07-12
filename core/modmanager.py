"""汎用のMinecraft MOD管理(ローカルjarライブラリ + サーバーへの配布)。

- ホスト側の modlib/ に jar(ばら=個別追加用ライブラリ)と
  modlib/packs/<名前>/ (modpack=jar群) を置く。
- サーバーの mods/ に対して「個別追加/削除」「modpack一括適用(同期)」を行う。
- mods/ は runtime_user 所有なので /tmp にSFTP → sudo で mv+chown する(moddeployと同方式)。

SQL共有連動の自動デプロイ(InvSync)は moddeploy.py が担当。こちらは手動のmod管理用。
mods場所・所有者は GameServerProfile.mods_dir / runtime_user から取る。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import paramiko


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
