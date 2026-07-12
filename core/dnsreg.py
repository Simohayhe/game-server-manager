"""LAN内DNS(ipamのPowerDNS)へのレコード自動登録。

phpIPAM/PowerDNSのレコードはipamホストのMySQL(phpipam DB)にあるため、
ipamへSSHしてmysqlコマンドでA/PTRを冪等に登録する。
"""
from __future__ import annotations

from dataclasses import dataclass

import paramiko


@dataclass
class DnsConfig:
    host: str            # ipamホスト(例 192.168.11.254)
    ssh_user: str
    ssh_password: str
    domain: str          # 例 example.com


class DnsRegError(Exception):
    pass


# IP変更に追随できるよう短めのTTL(秒)。7日等の長TTLはキャッシュで変更が反映されない
RECORD_TTL = 300


def _reverse_name(ip: str) -> tuple[str, str]:
    """IPv4 → (逆引きレコード名, 逆引きゾーン名)。/24前提。"""
    o = ip.split(".")
    if len(o) != 4:
        raise DnsRegError(f"IPv4アドレスではありません: {ip}")
    return (f"{o[3]}.{o[2]}.{o[1]}.{o[0]}.in-addr.arpa",
            f"{o[2]}.{o[1]}.{o[0]}.in-addr.arpa")


def register_host(cfg: DnsConfig, hostname: str, ip: str,
                  progress=lambda text: None) -> str:
    """hostname(短名)とIPからA/PTRレコードを登録し、FQDNを返す。既存ならスキップ。"""
    fqdn = hostname if "." in hostname else f"{hostname}.{cfg.domain}"
    ptr_name, rev_zone = _reverse_name(ip)

    script = f"""#!/bin/bash
set -e
FID=$(mysql -N -e "SELECT id FROM phpipam.domains WHERE name='{cfg.domain}';")
RID=$(mysql -N -e "SELECT id FROM phpipam.domains WHERE name='{rev_zone}';")
[ -n "$FID" ] || {{ echo "NG: zone {cfg.domain} not found"; exit 1; }}
A=$(mysql -N -e "SELECT COUNT(*) FROM phpipam.records WHERE name='{fqdn}' AND type='A';")
if [ "$A" -eq 0 ]; then
  mysql -e "INSERT INTO phpipam.records (domain_id, name, type, content, ttl, prio, disabled, auth) VALUES ($FID, '{fqdn}', 'A', '{ip}', {RECORD_TTL}, 0, 0, 1);"
  echo "A_ADDED"
else
  echo "A_EXISTS"
fi
if [ -n "$RID" ]; then
  P=$(mysql -N -e "SELECT COUNT(*) FROM phpipam.records WHERE name='{ptr_name}' AND type='PTR';")
  if [ "$P" -eq 0 ]; then
    mysql -e "INSERT INTO phpipam.records (domain_id, name, type, content, ttl, prio, disabled, auth) VALUES ($RID, '{ptr_name}', 'PTR', '{fqdn}', {RECORD_TTL}, 0, 0, 1);"
    echo "PTR_ADDED"
  else
    echo "PTR_EXISTS"
  fi
else
  echo "PTR_SKIP (zone {rev_zone} not found)"
fi
rec_control wipe-cache '{fqdn}' >/dev/null 2>&1 || true
rec_control wipe-cache '{ptr_name}' >/dev/null 2>&1 || true
echo "DNSREG_OK"
"""

    progress(f"DNS登録中: {fqdn} → {ip}")
    _run_script(cfg, script)
    return fqdn


def update_ip(cfg: DnsConfig, old_ip: str, new_ip: str,
              progress=lambda text: None) -> None:
    """IP変更に伴い、old_ipを指す全Aレコードとold_ipのPTRを新IPへ付け替える。"""
    old_ptr, old_zone = _reverse_name(old_ip)
    new_ptr, new_zone = _reverse_name(new_ip)

    if old_zone == new_zone:
        # 新名の既存PTRを消してから旧名をリネーム(重複防止)
        ptr_sql = (f"mysql -e \"DELETE FROM phpipam.records"
                   f" WHERE type='PTR' AND name='{new_ptr}';"
                   f" UPDATE phpipam.records SET name='{new_ptr}'"
                   f" WHERE type='PTR' AND name='{old_ptr}';\"")
    else:
        # 逆引きゾーンをまたぐ場合: 旧PTRを消し、新ゾーンがあれば作り直す
        ptr_sql = (
            f"OLDF=$(mysql -N -e \"SELECT content FROM phpipam.records"
            f" WHERE type='PTR' AND name='{old_ptr}' LIMIT 1;\")\n"
            f"mysql -e \"DELETE FROM phpipam.records WHERE type='PTR' AND name='{old_ptr}';\"\n"
            f"RID=$(mysql -N -e \"SELECT id FROM phpipam.domains WHERE name='{new_zone}';\")\n"
            f"if [ -n \"$RID\" ] && [ -n \"$OLDF\" ]; then\n"
            f"  mysql -e \"INSERT INTO phpipam.records (domain_id, name, type, content,"
            f" ttl, prio, disabled, auth) VALUES ($RID, '{new_ptr}', 'PTR', '$OLDF',"
            f" 604800, 0, 0, 1);\"\n"
            f"fi")

    script = f"""#!/bin/bash
set -e
mysql -e "UPDATE phpipam.records SET content='{new_ip}', ttl={RECORD_TTL} WHERE type='A' AND content='{old_ip}';"
{ptr_sql}
# 変更を即時反映させるためRecursorのキャッシュを消す(旧名/新名 両方)
for n in '{old_ptr}' '{new_ptr}'; do rec_control wipe-cache "$n" >/dev/null 2>&1 || true; done
for f in $(mysql -N -e "SELECT name FROM phpipam.records WHERE type='A' AND content='{new_ip}';"); do
  rec_control wipe-cache "$f" >/dev/null 2>&1 || true
done
echo DNSREG_OK
"""
    progress(f"DNSレコードを付け替え中: {old_ip} → {new_ip}")
    _run_script(cfg, script)


def set_a_record(cfg: DnsConfig, name: str, ip: str,
                 progress=lambda text: None) -> None:
    """指定FQDNのAレコードをipに設定(なければ作成)し、キャッシュを消す。"""
    fqdn = name if "." in name else f"{name}.{cfg.domain}"
    script = f"""#!/bin/bash
set -e
FID=$(mysql -N -e "SELECT id FROM phpipam.domains WHERE name='{cfg.domain}';")
[ -n "$FID" ] || {{ echo "NG: zone not found"; exit 1; }}
N=$(mysql -N -e "SELECT COUNT(*) FROM phpipam.records WHERE name='{fqdn}' AND type='A';")
if [ "$N" -eq 0 ]; then
  mysql -e "INSERT INTO phpipam.records (domain_id,name,type,content,ttl,prio,disabled,auth) VALUES ($FID,'{fqdn}','A','{ip}',{RECORD_TTL},0,0,1);"
else
  mysql -e "UPDATE phpipam.records SET content='{ip}',ttl={RECORD_TTL} WHERE name='{fqdn}' AND type='A';"
fi
rec_control wipe-cache '{fqdn}' >/dev/null 2>&1 || true
echo DNSREG_OK
"""
    progress(f"Aレコード更新: {fqdn} → {ip}")
    _run_script(cfg, script)


def replace_a_ip(cfg: DnsConfig, old_ip: str, new_ip: str,
                 progress=lambda text: None) -> None:
    """old_ipを指す全Aレコードをnew_ipに更新する(WAN IP変動時の一括追随用)。"""
    script = f"""#!/bin/bash
set -e
for f in $(mysql -N -e "SELECT name FROM phpipam.records WHERE type='A' AND content='{old_ip}';"); do
  rec_control wipe-cache "$f" >/dev/null 2>&1 || true
done
mysql -e "UPDATE phpipam.records SET content='{new_ip}',ttl={RECORD_TTL} WHERE type='A' AND content='{old_ip}';"
echo DNSREG_OK
"""
    progress(f"WAN IP追随: {old_ip} を指す全Aレコード → {new_ip}")
    _run_script(cfg, script)


def publish_server(cfg: DnsConfig, fqdn: str, wan_ip: str, external_port: int,
                   service: str = "minecraft", progress=lambda text: None) -> None:
    """サーバーを名前で外部公開する: A(fqdn→WAN) + SRV(ポート隠蔽)。

    プレイヤーは fqdn だけ入力すれば、SRVで external_port に自動で振り分けられる。
    """
    srv_name = f"_{service}._tcp.{fqdn}"
    srv_content = f"0 5 {external_port} {fqdn}."
    script = f"""#!/bin/bash
set -e
FID=$(mysql -N -e "SELECT id FROM phpipam.domains WHERE name='{cfg.domain}';")
[ -n "$FID" ] || {{ echo "NG: zone not found"; exit 1; }}
# A: fqdn -> WAN
N=$(mysql -N -e "SELECT COUNT(*) FROM phpipam.records WHERE name='{fqdn}' AND type='A';")
if [ "$N" -eq 0 ]; then
  mysql -e "INSERT INTO phpipam.records (domain_id,name,type,content,ttl,prio,disabled,auth) VALUES ($FID,'{fqdn}','A','{wan_ip}',{RECORD_TTL},0,0,1);"
else
  mysql -e "UPDATE phpipam.records SET content='{wan_ip}',ttl={RECORD_TTL} WHERE name='{fqdn}' AND type='A';"
fi
# SRV: _service._tcp.fqdn -> external_port
S=$(mysql -N -e "SELECT COUNT(*) FROM phpipam.records WHERE name='{srv_name}' AND type='SRV';")
if [ "$S" -eq 0 ]; then
  mysql -e "INSERT INTO phpipam.records (domain_id,name,type,content,ttl,prio,disabled,auth) VALUES ($FID,'{srv_name}','SRV','{srv_content}',{RECORD_TTL},NULL,0,1);"
else
  mysql -e "UPDATE phpipam.records SET content='{srv_content}',ttl={RECORD_TTL} WHERE name='{srv_name}' AND type='SRV';"
fi
rec_control wipe-cache '{fqdn}' >/dev/null 2>&1 || true
rec_control wipe-cache '{srv_name}' >/dev/null 2>&1 || true
echo DNSREG_OK
"""
    progress(f"公開DNS設定: {fqdn} (SRV→外部{external_port})")
    _run_script(cfg, script)


def unpublish_server(cfg: DnsConfig, fqdn: str, service: str = "minecraft",
                     progress=lambda text: None) -> None:
    """SRVレコードを削除して名前公開を解除する(Aレコードは残す)。"""
    srv_name = f"_{service}._tcp.{fqdn}"
    script = f"""#!/bin/bash
set -e
mysql -e "DELETE FROM phpipam.records WHERE name='{srv_name}' AND type='SRV';"
rec_control wipe-cache '{srv_name}' >/dev/null 2>&1 || true
echo DNSREG_OK
"""
    progress(f"公開解除(SRV削除): {srv_name}")
    _run_script(cfg, script)


def delete_srv(cfg: DnsConfig, srv_name: str, progress=lambda text: None) -> None:
    """任意のSRVレコード名を削除(古い/不正なSRVの掃除用)。"""
    script = (f'#!/bin/bash\nset -e\n'
              f'mysql -e "DELETE FROM phpipam.records WHERE name=\'{srv_name}\' '
              f'AND type=\'SRV\';"\necho DNSREG_OK\n')
    progress(f"SRV削除: {srv_name}")
    _run_script(cfg, script)


def _run_script(cfg: DnsConfig, script: str) -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(cfg.host, username=cfg.ssh_user,
                       password=cfg.ssh_password, timeout=15)
    except Exception as exc:
        raise DnsRegError(f"DNSホスト({cfg.host})にSSH接続できません: {exc}") from exc
    try:
        sftp = client.open_sftp()
        with sftp.open("/tmp/gsm_dnsreg.sh", "w") as f:
            f.write(script)
        sftp.close()
        stdin, stdout, _ = client.exec_command(
            "sudo -S bash /tmp/gsm_dnsreg.sh 2>&1", timeout=60)
        stdin.write(cfg.ssh_password + "\n")
        stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        if "DNSREG_OK" not in out:
            raise DnsRegError(f"DNS登録に失敗しました:\n{out[-400:]}")
    finally:
        client.close()
