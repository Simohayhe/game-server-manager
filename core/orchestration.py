"""VMとゲームサーバーをまたぐ複合操作。

GUIとWeb版で共通に使えるよう、UI非依存の関数として置く。
"""
from __future__ import annotations

import socket
import time

from .gameserver import GameServer
from .hyperv import HyperVManager


def start_server_with_vm(hyperv: HyperVManager, server: GameServer,
                         progress=lambda text: None,
                         boot_timeout: int = 240) -> None:
    """VMが停止していれば起動し、SSH到達を待ってからサービスを起動する。

    progress: 途中経過を通知するコールバック(UIスレッド安全である必要はない)。
    """
    profile = server.profile
    if profile.vm:
        vms = {v.name: v for v in hyperv.list_vms()}
        vm = vms.get(profile.vm)
        if vm is not None and vm.state != "Running":
            progress(f"VM {profile.vm} を起動中…")
            hyperv.start_vm(profile.vm)
            progress(f"VM {profile.vm} の起動完了を待機中(SSH応答待ち)…")
            _wait_for_port(profile.address, profile.ssh_port, boot_timeout)
    progress(f"{profile.display_name} のサービスを起動中…")
    server.start()


INDIVIDUALIZE_SCRIPT = """#!/bin/bash
set -e
hostnamectl set-hostname @HOSTNAME@
sed -i 's/^127\\.0\\.1\\.1.*/127.0.1.1 @HOSTNAME@/' /etc/hosts
grep -q '^127.0.1.1' /etc/hosts || echo '127.0.1.1 @HOSTNAME@' >> /etc/hosts
rm -f /etc/machine-id /var/lib/dbus/machine-id
systemd-machine-id-setup
ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A > /dev/null
NP=$(ls /etc/netplan/*.yaml | head -1)
cat > "$NP" <<EOF
network:
  ethernets:
    eth0:
      addresses:
      - @NEW_IP@/24
      nameservers:
        addresses:
        - @DNS@
        search: []
      routes:
      - to: default
        via: @GATEWAY@
  version: 2
EOF
chmod 600 "$NP"
sync
# machine-id変更を完全に反映させるため再起動する(journaldの書き込み先も切り替わる)
nohup sh -c 'sleep 2; reboot' >/dev/null 2>&1 &
echo INDIVIDUALIZE_OK
"""


def individualize_clone(template_ip: str, ssh_user: str, ssh_password: str,
                        hostname: str, new_ip: str,
                        gateway: str, dns: str,
                        progress=lambda text: None, timeout: int = 180) -> None:
    """クローン直後のVM(テンプレートのIPで起動中)を個体化する。

    ホスト名・machine-id・SSHホスト鍵を新しくし、netplanに新IPを書いて再起動する。
    完了後、新IPでSSHが応答するまで待つ。
    """
    import paramiko

    script = (INDIVIDUALIZE_SCRIPT
              .replace("@HOSTNAME@", hostname)
              .replace("@NEW_IP@", new_ip)
              .replace("@GATEWAY@", gateway)
              .replace("@DNS@", dns))

    progress(f"クローンに接続中({template_ip})…")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(template_ip, username=ssh_user, password=ssh_password, timeout=15)
    try:
        sftp = client.open_sftp()
        with sftp.open("/tmp/gsm_individualize.sh", "w") as f:
            f.write(script)
        sftp.close()
        stdin, stdout, _ = client.exec_command(
            "sudo -S bash /tmp/gsm_individualize.sh 2>&1", timeout=120)
        stdin.write(ssh_password + "\n")
        stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        if "INDIVIDUALIZE_OK" not in out:
            raise RuntimeError(f"個体化に失敗しました:\n{out[-500:]}")
    finally:
        client.close()

    progress(f"新IP({new_ip})での応答を待機中…")
    _wait_for_port(new_ip, 22, timeout)


CHANGE_IP_SCRIPT = """#!/bin/bash
set -e
NP=$(ls /etc/netplan/*.yaml | head -1)
cp "$NP" /home/master/netplan_before_ip_change.yaml 2>/dev/null || true
cat > "$NP" <<EOF
network:
  ethernets:
    eth0:
      addresses:
      - @NEW_IP@/24
      nameservers:
        addresses:
        - @DNS@
        search: []
      routes:
      - to: default
        via: @GATEWAY@
  version: 2
EOF
chmod 600 "$NP"
sync
nohup sh -c 'sleep 2; reboot' >/dev/null 2>&1 &
echo CHANGE_IP_OK
"""


def change_vm_ip(current_ip: str, ssh_user: str, ssh_password: str,
                 new_ip: str, gateway: str, dns: str,
                 progress=lambda text: None, timeout: int = 240) -> None:
    """稼働中のLinux VMのIPをnetplan書き換え+再起動で変更する。

    完了後、新IPでSSHが応答するまで待つ。
    """
    import paramiko

    script = (CHANGE_IP_SCRIPT
              .replace("@NEW_IP@", new_ip)
              .replace("@GATEWAY@", gateway)
              .replace("@DNS@", dns))

    progress(f"VM({current_ip})に接続してIPを変更中…")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(current_ip, username=ssh_user, password=ssh_password, timeout=15)
    try:
        sftp = client.open_sftp()
        with sftp.open("/tmp/gsm_change_ip.sh", "w") as f:
            f.write(script)
        sftp.close()
        stdin, stdout, _ = client.exec_command(
            "sudo -S bash /tmp/gsm_change_ip.sh 2>&1", timeout=60)
        stdin.write(ssh_password + "\n")
        stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        if "CHANGE_IP_OK" not in out:
            raise RuntimeError(f"IP変更スクリプトが失敗しました:\n{out[-400:]}")
    finally:
        client.close()

    progress(f"再起動して新IP({new_ip})での応答を待機中…")
    _wait_for_port(new_ip, 22, timeout)


def check_ip_free(ip: str) -> bool:
    """指定IPが未使用らしいことを確認する(SSH到達で簡易判定)。"""
    try:
        with socket.create_connection((ip, 22), timeout=2):
            return False
    except OSError:
        return True


def _wait_for_port(host: str, port: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return
        except OSError:
            time.sleep(5)
    raise RuntimeError(
        f"{host}:{port} が{timeout}秒以内に応答しませんでした(VMの起動に失敗?)")
