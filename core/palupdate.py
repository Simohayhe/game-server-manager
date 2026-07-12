"""Palworld(VM)専用サーバーの更新チェック/更新(VM上のSteamCMDをSSHで実行)。

App 2394010。VMに apt で入れた steamcmd(/usr/games/steamcmd)と、
<install>/steamapps/appmanifest_2394010.acf の buildid で判定する。
更新は「停止→app_update→起動」をSSHで実行。
"""
from __future__ import annotations

import re

import paramiko

APP_ID = 2394010
STEAMCMD = "/usr/games/steamcmd"


def _ssh(profile):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(profile.address, username=profile.ssh_user,
              password=profile.ssh_password, port=profile.ssh_port, timeout=15)
    return c


def _run(c, cmd, timeout=120, sudo_pw=None):
    stdin, stdout, _ = c.exec_command(cmd, timeout=timeout, get_pty=bool(sudo_pw))
    if sudo_pw:
        stdin.write(sudo_pw + "\n")
        stdin.flush()
    return stdout.read().decode("utf-8", "replace")


def check(profile) -> dict:
    """導入buildと最新buildを比較(SSH)。"""
    c = _ssh(profile)
    try:
        acf = f"{profile.install_dir.rstrip('/')}/steamapps/appmanifest_{APP_ID}.acf"
        inst_out = _run(c, f"grep -m1 buildid '{acf}' 2>/dev/null", timeout=20)
        m = re.search(r'"buildid"\s*"(\d+)"', inst_out)
        installed = m.group(1) if m else None
        latest_out = _run(
            c, f"{STEAMCMD} +login anonymous +app_info_update 1 "
               f"+app_info_print {APP_ID} +quit 2>/dev/null", timeout=240)
        lm = re.search(r'"public"\s*\{\s*"buildid"\s*"(\d+)"', latest_out)
        latest = lm.group(1) if lm else None
    finally:
        c.close()
    return {"installed": installed, "latest": latest,
            "update_available": bool(installed and latest and installed != latest)}


def update(profile, progress=lambda t: None, timeout=5400) -> str:
    """停止→SteamCMD更新→起動(SSH)。進捗を progress へ流す。"""
    c = _ssh(profile)
    try:
        pw = profile.ssh_password
        svc = profile.service or "palworld"
        install = profile.install_dir.rstrip("/")
        progress("サーバーを停止中…")
        _run(c, f"sudo -S systemctl stop {svc} 2>&1", timeout=90, sudo_pw=pw)
        progress("SteamCMDで更新中…(数分)")
        cmd = (f"{STEAMCMD} +force_install_dir '{install}' +login anonymous "
               f"+app_update {APP_ID} validate +quit")
        stdin, stdout, _ = c.exec_command(cmd, timeout=timeout, get_pty=True)
        last = ""
        for line in iter(stdout.readline, ""):
            s = line.rstrip()
            if s and (("%" in s) or ("Update state" in s) or ("Success" in s)
                      or ("ERROR" in s)):
                last = s
                progress(s[:120])
        progress("サーバーを起動中…")
        _run(c, f"sudo -S systemctl start {svc} 2>&1", timeout=90, sudo_pw=pw)
        # 新buildid
        acf = f"{install}/steamapps/appmanifest_{APP_ID}.acf"
        out = _run(c, f"grep -m1 buildid '{acf}' 2>/dev/null", timeout=20)
        m = re.search(r'"buildid"\s*"(\d+)"', out)
        return m.group(1) if m else "?"
    finally:
        c.close()
