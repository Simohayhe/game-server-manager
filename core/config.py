"""config.yamlの読み込み。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .arkhost import ArkHostConfig
from .palhost import PalHostConfig
from .backup import BackupConfig
from .dnsreg import DnsConfig
from .gameserver import GameServerProfile, RconConfig
from .moddeploy import ModSyncConfig
from .sqlshare import MySQLConfig


@dataclass
class HyperVConfig:
    mode: str = "ssh"              # "ssh"(別マシンのホスト) or "local"(このPC)
    host: str | None = None
    user: str | None = None
    port: int = 22
    key: str | None = None
    password: str | None = None


@dataclass
class NetworkConfig:
    """VM用のLANネットワーク設定(/24前提)。"""
    prefix: str = "192.168.11"         # サブネットの先頭3オクテット
    vm_range: tuple[int, int] = (100, 199)  # VMに割り当てる第4オクテットの範囲
    gateway: str = "192.168.11.1"

    @property
    def subnet_text(self) -> str:
        return f"{self.prefix}.0/24"

    def full_ip(self, text: str) -> str:
        """'101'のような第4オクテット、またはフルIPをフルIPに正規化する。"""
        text = text.strip()
        if "." not in text:
            octet = int(text)
            if not 1 <= octet <= 254:
                raise ValueError(f"第4オクテットが不正です: {octet}")
            return f"{self.prefix}.{octet}"
        return text


@dataclass
class PublishConfig:
    """外部公開(FQDNで外部アクセス)の設定。"""
    public_name: str                   # 例 mc.example.com(WAN IPを指す)
    glue_hosts: list[str]              # レジストラ登録のNS(glue、WAN IP)
    registrar: str = ""                # 通知に出すレジストラ名/手順
    last_wan_ip: str = ""              # 最後に確認したWAN IP(変動検知・一括追随用)


@dataclass
class AppConfig:
    hyperv: HyperVConfig
    servers: list[GameServerProfile]
    mysql: MySQLConfig | None = None   # サーバー間データ共有用(SQLタブ)。未設定可
    dns: DnsConfig | None = None       # LAN内DNS自動登録用(未設定可)
    network: NetworkConfig = None      # VM用ネットワーク既定(未設定時はデフォルト値)
    publish: PublishConfig | None = None  # 外部公開ヘルスチェック用(未設定可)
    mod_sync: ModSyncConfig | None = None  # SQL共有連動のMOD自動デプロイ(未設定可)
    ark_hosts: list = field(default_factory=list)  # ホストで動くARKサーバー群(複数マップ対応)
    ark_steamcmd: str = ""                 # SteamCMD.exe のパス(更新用。ARK/Palworldで共有)
    pal_hosts: list = field(default_factory=list)  # ホストで動くPalworldサーバー群
    backup: BackupConfig | None = None     # バックアップ設定(未設定可)
    curseforge_api_key: str = ""           # CurseForge APIキー(mod検索/導入用。未設定可)


class ConfigError(Exception):
    pass


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"設定ファイルが見つかりません: {path}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    hv_raw = raw.get("hyperv") or {}
    hyperv = HyperVConfig(
        mode=hv_raw.get("mode", "ssh"),
        host=hv_raw.get("host"),
        user=hv_raw.get("user"),
        port=hv_raw.get("port", 22),
        key=hv_raw.get("key"),
        password=hv_raw.get("password"),
    )
    if hyperv.mode == "ssh" and not hyperv.host:
        raise ConfigError("hyperv.mode: ssh の場合は hyperv.host を設定してください")

    servers: list[GameServerProfile] = []
    for name, s in (raw.get("servers") or {}).items():
        ssh_raw = s.get("ssh") or {}
        rcon_raw = s.get("rcon")
        rcon = None
        if rcon_raw:
            rcon = RconConfig(port=int(rcon_raw["port"]), password=str(rcon_raw["password"]))
        if not s.get("address"):
            raise ConfigError(f"servers.{name}: address(VMのIPアドレス)は必須です")
        if not ssh_raw.get("user"):
            raise ConfigError(f"servers.{name}: ssh.user は必須です")
        servers.append(GameServerProfile(
            name=name,
            display_name=s.get("display_name", name),
            address=s["address"],
            game=s.get("game", "minecraft"),
            fqdn=s.get("fqdn"),
            vm=s.get("vm"),
            ssh_user=ssh_raw["user"],
            ssh_port=int(ssh_raw.get("port", 22)),
            ssh_key=ssh_raw.get("key"),
            ssh_password=ssh_raw.get("password"),
            service=s.get("service"),
            rcon=rcon,
            players_command=s.get("players_command", "list"),
            game_port=int(s["game_port"]) if s.get("game_port") else None,
            external_port=int(s["external_port"]) if s.get("external_port") else None,
            install_dir=s.get("install_dir", "/opt/minecraft"),
            runtime_user=s.get("runtime_user", "minecraft"),
            config_file=s.get("config_file", "server.properties"),
            version_pattern=s.get("version_pattern"),
            players_pattern=s.get("players_pattern"),
            commands=s.get("commands") or {},
        ))

    mysql = None
    my_raw = raw.get("mysql")
    if my_raw:
        for key in ("host", "user", "password"):
            if not my_raw.get(key):
                raise ConfigError(f"mysql.{key} は必須です")
        mysql = MySQLConfig(
            host=my_raw["host"],
            port=int(my_raw.get("port", 3306)),
            user=my_raw["user"],
            password=str(my_raw["password"]),
            prefix=my_raw.get("prefix", "gsdata_"),
        )

    dns = None
    dns_raw = raw.get("dns")
    if dns_raw:
        ssh_raw = dns_raw.get("ssh") or {}
        for key, val in (("host", dns_raw.get("host")), ("domain", dns_raw.get("domain")),
                         ("ssh.user", ssh_raw.get("user")),
                         ("ssh.password", ssh_raw.get("password"))):
            if not val:
                raise ConfigError(f"dns.{key} は必須です")
        dns = DnsConfig(
            host=dns_raw["host"],
            ssh_user=ssh_raw["user"],
            ssh_password=str(ssh_raw["password"]),
            domain=dns_raw["domain"],
        )

    network = NetworkConfig()
    net_raw = raw.get("network")
    if net_raw:
        subnet = net_raw.get("subnet", "192.168.11.0/24")
        prefix = ".".join(subnet.split("/")[0].split(".")[:3])
        rng = str(net_raw.get("vm_range", "100-199"))
        lo, _, hi = rng.partition("-")
        network = NetworkConfig(
            prefix=prefix,
            vm_range=(int(lo), int(hi or lo)),
            gateway=net_raw.get("gateway", f"{prefix}.1"),
        )

    publish = None
    pub_raw = raw.get("publish")
    if pub_raw and pub_raw.get("public_name"):
        publish = PublishConfig(
            public_name=pub_raw["public_name"],
            glue_hosts=list(pub_raw.get("glue_hosts") or []),
            registrar=pub_raw.get("registrar", ""),
            last_wan_ip=str(pub_raw.get("last_wan_ip", "")),
        )

    mod_sync = None
    ms_raw = raw.get("mod_sync")
    if ms_raw:
        mod_sync = ModSyncConfig(
            enabled=bool(ms_raw.get("enabled", False)),
            modcache_dir=ms_raw.get("modcache_dir", "modcache"),
            server_mods_dir=ms_raw.get("server_mods_dir", "/opt/minecraft/mods"),
            server_config_dir=ms_raw.get("server_config_dir", "/opt/minecraft/config"),
            runtime_user=ms_raw.get("runtime_user", "minecraft"),
            dependency_jars=list(ms_raw.get("dependency_jars") or ["fabric-api.jar"]),
            sync_jar=ms_raw.get("sync_jar", "invsyncmod.jar"),
        )

    # ARK: ark_hosts(リスト)推奨。後方互換で ark_host(単一)も受ける。
    ark_hosts: list[ArkHostConfig] = []
    ah_list = raw.get("ark_hosts")
    if not ah_list and raw.get("ark_host"):
        ah_list = [raw.get("ark_host")]
    for ah_raw in (ah_list or []):
        if not ah_raw:
            continue
        install_dir = ah_raw.get("install_dir", "")
        exe_path = ah_raw.get("exe_path", "")
        config_dir = ah_raw.get("config_dir", "")
        if install_dir:                       # インストール指定があればパスを導出
            root = Path(install_dir)
            exe_path = exe_path or str(
                root / "ShooterGame" / "Binaries" / "Win64" / "ArkAscendedServer.exe")
            config_dir = config_dir or str(
                root / "ShooterGame" / "Saved" / "Config" / "WindowsServer")
        if not exe_path:
            continue
        ark_hosts.append(ArkHostConfig(
            display_name=ah_raw.get("display_name", "ARK (ホスト)"),
            exe_path=exe_path,
            launch_args=ah_raw.get("launch_args", ""),
            process_name=ah_raw.get("process_name", "ArkAscendedServer"),
            config_dir=config_dir,
            rcon_host=ah_raw.get("rcon_host", "127.0.0.1"),
            install_dir=install_dir,
        ))

    # Palworld(ホストで動く)
    pal_hosts: list[PalHostConfig] = []
    for ph_raw in (raw.get("pal_hosts") or []):
        if not ph_raw or not ph_raw.get("install_dir"):
            continue
        pal_hosts.append(PalHostConfig(
            display_name=ph_raw.get("display_name", "Palworld"),
            install_dir=ph_raw["install_dir"],
            launch_args=ph_raw.get(
                "launch_args",
                "-useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS"),
            process_name=ph_raw.get("process_name", "PalServer-Win64-Shipping"),
            rcon_host=ph_raw.get("rcon_host", "127.0.0.1"),
        ))

    backup = None
    bk_raw = raw.get("backup")
    if bk_raw:
        backup = BackupConfig(
            path=bk_raw.get("path", r"C:\GameBackups"),
            keep=int(bk_raw.get("keep", 10)),
            compress=bool(bk_raw.get("compress", True)),
        )

    cf_raw = raw.get("curseforge") or {}
    curseforge_api_key = str(cf_raw.get("api_key") or "")

    return AppConfig(hyperv=hyperv, servers=servers, mysql=mysql, dns=dns,
                     network=network, publish=publish, mod_sync=mod_sync,
                     curseforge_api_key=curseforge_api_key,
                     ark_hosts=ark_hosts, ark_steamcmd=raw.get("ark_steamcmd", ""),
                     pal_hosts=pal_hosts, backup=backup)
