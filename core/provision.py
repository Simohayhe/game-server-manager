"""新しいVMへのゲームサーバー自動構築(プロビジョニング)。

ゲーム固有の構築手順は provisioners/*.yaml のテンプレートに持たせ、
このモジュールは「テンプレートを描画してSSHで流し込む」汎用エンジンに徹する。
プレースホルダは {{name}} 形式(bashの ${} や awk の {} と衝突しないため)。
"""
from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass, field
from pathlib import Path

import paramiko
import yaml


@dataclass
class ProvisionTemplate:
    id: str
    display_name: str          # 例: "Minecraft (Fabric)"
    description: str
    mc_version: str = ""       # 構築するゲームバージョン(ウィザード表示用)
    defaults: dict = field(default_factory=dict)   # service/ポート等の既定値
    profile_extra: dict = field(default_factory=dict)  # config.yamlプロファイル固定項目
    script: str = ""

    @property
    def label(self) -> str:
        """ウィザードのドロップダウン表示名(バージョン付き)。"""
        return f"{self.display_name}  {self.mc_version}".rstrip()


class ProvisionError(Exception):
    pass


def templates_dir() -> Path:
    from .paths import bundle_dir
    return bundle_dir() / "provisioners"


def load_templates(dir_path: str | Path | None = None) -> list[ProvisionTemplate]:
    path = Path(dir_path) if dir_path else templates_dir()
    templates = []
    if not path.is_dir():
        return templates
    for p in sorted(path.glob("*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        templates.append(ProvisionTemplate(
            id=raw["id"],
            display_name=raw["display_name"],
            description=raw.get("description", ""),
            mc_version=str(raw.get("mc_version", "")),
            defaults=raw.get("defaults") or {},
            profile_extra=raw.get("profile") or {},
            script=raw["script"],
        ))
    return templates


def render_script(template: ProvisionTemplate, params: dict) -> str:
    script = template.script
    for key, value in params.items():
        script = script.replace("{{" + key + "}}", str(value))
    leftover = sorted(set(re.findall(r"\{\{(\w+)\}\}", script)))
    if leftover:
        raise ProvisionError(f"テンプレートの未指定パラメータ: {', '.join(leftover)}")
    return script


def generate_password(length: int = 20) -> str:
    return "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def provision(host: str, ssh_user: str, ssh_password: str, script: str,
              progress=lambda line: None, timeout: int = 1800) -> str:
    """構築スクリプトを対象ホストにアップロードしてsudo実行する。

    progress: 出力1行ごとに呼ばれる。戻り値は全ログ。
    失敗時は ProvisionError(末尾ログ付き)。
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=ssh_user, password=ssh_password, timeout=15)
    except Exception as exc:
        raise ProvisionError(f"{host} にSSH接続できません: {exc}") from exc
    try:
        sftp = client.open_sftp()
        with sftp.open("/tmp/gsm_provision.sh", "w") as f:
            f.write(script)
        sftp.close()

        stdin, stdout, _ = client.exec_command(
            "sudo -S bash /tmp/gsm_provision.sh 2>&1", timeout=timeout)
        stdin.write(ssh_password + "\n")
        stdin.flush()

        lines: list[str] = []
        for line in stdout:
            line = line.rstrip("\n")
            # sudoのパスワードプロンプトはノイズなので抑制
            if "[sudo]" in line or line.strip() == "Password:":
                continue
            lines.append(line)
            progress(line)
        rc = stdout.channel.recv_exit_status()
        log = "\n".join(lines)
        if rc != 0:
            tail = "\n".join(lines[-15:])
            raise ProvisionError(f"構築スクリプトが失敗しました(exit {rc}):\n{tail}")
        return log
    finally:
        client.close()


def append_profile_to_config(config_path: str | Path, name: str, profile: dict) -> None:
    """config.yamlの末尾(servers:配下)にプロファイルを追記する。

    servers: が最後のトップレベルキーである前提(現行config.yamlの構成)。
    追記後にロード検証し、失敗したら元に戻す。
    """
    from .config import load_config  # 循環import回避のため遅延

    path = Path(config_path)
    original = path.read_text(encoding="utf-8")

    block = yaml.safe_dump({name: profile}, allow_unicode=True,
                           default_flow_style=False, sort_keys=False)
    indented = "\n".join("  " + line if line.strip() else line
                         for line in block.splitlines())
    new_text = original.rstrip("\n") + "\n\n" + indented + "\n"
    path.write_text(new_text, encoding="utf-8")
    try:
        cfg = load_config(path)
        if not any(p.name == name for p in cfg.servers):
            raise ProvisionError(
                f"追記した {name} がconfigに現れません(インデント崩れ?)")
    except Exception:
        path.write_text(original, encoding="utf-8")  # ロールバック
        raise
