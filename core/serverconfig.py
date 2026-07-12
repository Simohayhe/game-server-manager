"""ゲームサーバーの設定ファイル(まずMinecraftのserver.properties)の読み書き。

SSHでファイル取得 → GUIで編集 → /tmp にSFTP → sudo で書き戻し → 再起動。
key=value形式(.properties)を、コメント・順序・未知キーを保持したまま更新する。
将来ini(セクション付き)対応も見据え、パースは Properties クラスに閉じ込める。
設定ファイルの場所は GameServerProfile.config_path(install_dir/config_file)。
"""
from __future__ import annotations

import paramiko


class ServerConfigError(Exception):
    pass


class Properties:
    """server.properties 等の key=value を、順序・コメント保持で扱う。"""

    def __init__(self, text: str):
        norm = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = norm.split("\n")
        self._index: dict[str, int] = {}
        for i, line in enumerate(self.lines):
            s = line.lstrip()
            if s and not s.startswith("#") and not s.startswith("!") and "=" in s:
                key = line.split("=", 1)[0].strip()
                if key:
                    self._index[key] = i

    def keys(self) -> list[str]:
        return list(self._index.keys())

    def get(self, key: str, default: str = "") -> str:
        i = self._index.get(key)
        if i is None:
            return default
        line = self.lines[i]
        return line.split("=", 1)[1] if "=" in line else default

    def set(self, key: str, value: str) -> None:
        i = self._index.get(key)
        if i is None:
            self.lines.append(f"{key}={value}")
            self._index[key] = len(self.lines) - 1
        else:
            self.lines[i] = f"{key}={value}"

    def text(self) -> str:
        out = "\n".join(self.lines)
        if not out.endswith("\n"):
            out += "\n"
        return out


def _connect(profile) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(profile.address, username=profile.ssh_user,
                  password=profile.ssh_password, port=profile.ssh_port, timeout=15)
    except Exception as exc:
        raise ServerConfigError(
            f"{profile.address} にSSH接続できません: {exc}") from exc
    return c


def read_config(profile) -> str:
    """サーバーの設定ファイル本文を返す。"""
    client = _connect(profile)
    try:
        path = profile.config_path
        _, stdout, stderr = client.exec_command(f"cat '{path}'", timeout=20)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if not out and err:
            raise ServerConfigError(f"{path} を読めません: {err.strip()}")
        return out
    finally:
        client.close()


def _sudo_run(client: paramiko.SSHClient, password: str, script: str) -> str:
    sftp = client.open_sftp()
    with sftp.open("/tmp/gsm_serverconfig.sh", "w") as f:
        f.write(script)
    sftp.close()
    stdin, stdout, _ = client.exec_command(
        "sudo -S bash /tmp/gsm_serverconfig.sh 2>&1", timeout=120)
    stdin.write(password + "\n")
    stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    if "SVCFG_OK" not in out:
        raise ServerConfigError(f"設定の書き込みに失敗しました:\n{out[-800:]}")
    return out


def write_config(profile, text: str, restart: bool = True,
                 progress=lambda t: None) -> None:
    """設定ファイル本文を書き戻し、必要なら再起動する。"""
    client = _connect(profile)
    try:
        progress(f"{profile.display_name}: 設定を書き込み中…")
        sftp = client.open_sftp()
        with sftp.open("/tmp/gsm_server_cfg.txt", "w") as f:
            f.write(text)
        sftp.close()
        path = profile.config_path
        restart_cmd = (f"systemctl restart {profile.service}"
                       if restart and profile.service else "true")
        script = f"""#!/bin/bash
set -e
mv -f /tmp/gsm_server_cfg.txt '{path}'
chown {profile.runtime_user}:{profile.runtime_user} '{path}'
{restart_cmd}
echo SVCFG_OK
"""
        progress(f"{profile.display_name}: 反映"
                 f"{'・再起動' if restart and profile.service else ''}中…")
        _sudo_run(client, profile.ssh_password, script)
    finally:
        client.close()
