"""Palworld の PalWorldSettings.ini(OptionSettings=(...) 1行形式)を編集する。

VM上のファイルなのでSSH(SFTP)で読み書きする。OptionSettings はカンマ区切りの
key=value が1行に並ぶ(文字列は"で囲む、CrossplayPlatforms等は入れ子括弧)。
単純キー(数値/真偽/文字列/列挙)だけを正規表現で安全に読み書きし、入れ子括弧には触れない。
"""
from __future__ import annotations

import re

import paramiko

REL_CONFIG = "Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"


class PalOptions:
    def __init__(self, text: str):
        self.full = text
        m = re.search(r'OptionSettings=\((.*)\)', text, re.DOTALL)
        if m:
            self._pre = text[:m.start(1)]
            self._body = m.group(1)
            self._post = text[m.end(1):]
        else:                                  # 無ければ最低限の枠を作る
            self._pre = "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=("
            self._body = ""
            self._post = ")\n"

    def get(self, key: str, default=None):
        m = re.search(rf'(?:^|,){re.escape(key)}=("([^"]*)"|[^,()]*)', self._body)
        if not m:
            return default
        return m.group(2) if m.group(2) is not None else m.group(1)

    def set(self, key: str, value: str) -> None:
        """key=value を差し替え(無ければ末尾に追記)。valueは呼び出し側で整形済み。"""
        pat = rf'((?:^|,){re.escape(key)}=)("[^"]*"|[^,()]*)'
        if re.search(pat, self._body):
            self._body = re.sub(pat, lambda mm: mm.group(1) + value, self._body, count=1)
        else:
            self._body += ("," if self._body else "") + f"{key}={value}"

    def text(self) -> str:
        return self._pre + self._body + self._post


def _ssh(profile):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(profile.address, username=profile.ssh_user,
              password=profile.ssh_password, port=profile.ssh_port, timeout=15)
    return c


def _config_path(profile) -> str:
    return f"{profile.install_dir.rstrip('/')}/{REL_CONFIG}"


def read(profile) -> PalOptions:
    c = _ssh(profile)
    try:
        sftp = c.open_sftp()
        with sftp.open(_config_path(profile)) as f:
            text = f.read().decode("utf-8", "replace")
        sftp.close()
    finally:
        c.close()
    return PalOptions(text)


def write(profile, options: PalOptions, restart: bool = False,
          progress=lambda t: None) -> None:
    c = _ssh(profile)
    try:
        sftp = c.open_sftp()
        with sftp.open(_config_path(profile), "w") as f:
            f.write(options.text())
        sftp.close()
        progress("PalWorldSettings.ini を保存しました")
        if restart and profile.service:
            progress("Palworldサーバーを再起動して反映…")
            stdin, stdout, _ = c.exec_command(
                f"sudo -S systemctl restart {profile.service} 2>&1", timeout=60)
            stdin.write(profile.ssh_password + "\n")
            stdin.flush()
            stdout.read()
    finally:
        c.close()
