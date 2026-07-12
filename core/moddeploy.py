"""SQL共有グループ連動のMOD自動デプロイ(InvSync)。

グループにサーバーを追加したら、そのサーバーの mods/ に fabric-api と invsyncmod を
配置し、config/invsyncmod.properties にグループDBの接続情報を書いて再起動する。
グループから除外/グループ削除時は invsyncmod と config を削除して再起動する。

MODファイルはGSMホスト上の modcache/ に置いたローカルjarを使う(ビルド済みを配布)。
mods/ は minecraft ユーザー所有なので、/tmp にSFTP→sudoで移動+chownする。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import paramiko


@dataclass
class ModSyncConfig:
    enabled: bool = False
    modcache_dir: str = "modcache"          # GSMディレクトリからの相対 or 絶対
    server_mods_dir: str = "/opt/minecraft/mods"
    server_config_dir: str = "/opt/minecraft/config"
    runtime_user: str = "minecraft"
    dependency_jars: list[str] = field(default_factory=lambda: ["fabric-api.jar"])
    sync_jar: str = "invsyncmod.jar"


class ModDeployError(Exception):
    pass


def _connect(profile) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(profile.address, username=profile.ssh_user,
                  password=profile.ssh_password, port=profile.ssh_port, timeout=15)
    except Exception as exc:
        raise ModDeployError(f"{profile.address} にSSH接続できません: {exc}") from exc
    return c


def _sudo_run(client: paramiko.SSHClient, password: str, script: str) -> str:
    sftp = client.open_sftp()
    with sftp.open("/tmp/gsm_moddeploy.sh", "w") as f:
        f.write(script)
    sftp.close()
    stdin, stdout, _ = client.exec_command(
        "sudo -S bash /tmp/gsm_moddeploy.sh 2>&1", timeout=180)
    stdin.write(password + "\n")
    stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    if "MODDEPLOY_OK" not in out:
        raise ModDeployError(f"MOD操作に失敗しました:\n{out[-500:]}")
    return out


def _properties(profile, group_info: dict) -> str:
    """invsyncmod.properties を生成する。server.name はサーバー固有(=プロファイル名)。"""
    return (
        f"server.name={profile.name}\n"
        f"db.host={group_info['host']}\n"
        f"db.port={group_info['port']}\n"
        f"db.name={group_info['database']}\n"
        f"db.user={group_info['user']}\n"
        f"db.password={group_info['password']}\n"
        f"db.pool.max=10\n"
        f"db.pool.timeout=30000\n"
    )


def install(profile, group_info: dict, cfg: ModSyncConfig, base_dir: Path,
            progress=lambda t: None) -> None:
    """サーバーに fabric-api + invsyncmod を配置し、config を書いて再起動する。"""
    cache = Path(cfg.modcache_dir)
    if not cache.is_absolute():
        cache = Path(base_dir) / cfg.modcache_dir
    jars = list(cfg.dependency_jars) + [cfg.sync_jar]
    for jar in jars:
        if not (cache / jar).exists():
            raise ModDeployError(
                f"{cache / jar} がありません(MODをビルドしてmodcacheに置いてください)")

    progress(f"{profile.display_name}: MODファイルを転送中…")
    client = _connect(profile)
    try:
        sftp = client.open_sftp()
        for jar in jars:
            sftp.put(str(cache / jar), f"/tmp/{jar}")
        with sftp.open("/tmp/invsyncmod.properties", "w") as f:
            f.write(_properties(profile, group_info))
        sftp.close()

        moves = "\n".join(
            f"mv -f /tmp/{jar} '{cfg.server_mods_dir}/{jar}'" for jar in jars)
        script = f"""#!/bin/bash
set -e
mkdir -p '{cfg.server_mods_dir}' '{cfg.server_config_dir}'
{moves}
mv -f /tmp/invsyncmod.properties '{cfg.server_config_dir}/invsyncmod.properties'
chown -R {cfg.runtime_user}:{cfg.runtime_user} '{cfg.server_mods_dir}' '{cfg.server_config_dir}'
systemctl restart {profile.service}
echo MODDEPLOY_OK
"""
        progress(f"{profile.display_name}: 配置してサーバー再起動中…")
        _sudo_run(client, profile.ssh_password, script)
    finally:
        client.close()


def uninstall(profile, cfg: ModSyncConfig, progress=lambda t: None) -> None:
    """invsyncmod と config を削除して再起動する(fabric-apiは共有のため残す)。"""
    progress(f"{profile.display_name}: MODを削除中…")
    client = _connect(profile)
    try:
        script = f"""#!/bin/bash
set -e
rm -f '{cfg.server_mods_dir}/{cfg.sync_jar}'
rm -f '{cfg.server_config_dir}/invsyncmod.properties'
systemctl restart {profile.service}
echo MODDEPLOY_OK
"""
        _sudo_run(client, profile.ssh_password, script)
    finally:
        client.close()
