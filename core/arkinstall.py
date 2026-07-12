"""ARK(ASA)を『マップ(サーバー)ごとの個別インストール』に分割する補助。

既存の1インストール(Server1)を各マップ用フォルダへコピー(robocopy)して、
config.yaml の ark_hosts を各マップ専用 install_dir に書き換える。以降はマップ単位で
SteamCMD更新・設定・セーブが独立する。ダウンロードより既存コピーの方が速く確実。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def target_dir(base: str | Path, map_label: str) -> Path:
    return Path(base) / map_label


def copy_install(source: str | Path, dest: str | Path,
                 progress=lambda t: None, timeout: int = 7200) -> str:
    """source インストールを dest へ丸ごとコピー(robocopy /E /MT)。"""
    source, dest = Path(source), Path(dest)
    if not source.exists():
        raise FileNotFoundError(f"コピー元が見つかりません: {source}")
    dest.mkdir(parents=True, exist_ok=True)
    progress(f"コピー中: {source} → {dest}(約14GB)")
    args = ["robocopy", str(source), str(dest),
            "/E", "/MT:16", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NP", "/NC", "/BYTES"]
    proc = subprocess.run(args, capture_output=True, text=True,
                          errors="replace", timeout=timeout)
    if proc.returncode >= 8:                  # robocopyは0-7が成功、8以上が失敗
        raise RuntimeError(
            f"robocopy失敗(code {proc.returncode}):\n{(proc.stdout or '')[-600:]}")
    progress(f"コピー完了: {dest.name}")
    return str(dest)


# ---------------------------------------------------------------------------
# config.yaml の ark_hosts ブロック書き換え
# ---------------------------------------------------------------------------
def _gen_block(entries: list[dict]) -> list[str]:
    """entries: display_name / launch_args / rcon_host / install_dir。"""
    out = ["ark_hosts:"]
    for e in entries:
        la = str(e["launch_args"]).replace("'", "''")   # YAML単一引用の中の ' は '' に
        dn = str(e["display_name"]).replace("'", "''")
        out.append(f"  - display_name: '{dn}'")
        out.append(f"    install_dir: '{e['install_dir']}'")
        out.append(f"    launch_args: '{la}'")
        out.append(f"    rcon_host: {e.get('rcon_host', '127.0.0.1')}")
    return out


def rewrite_installs(config_path: str | Path, entries: list[dict]) -> None:
    """config.yaml の ark_hosts: ブロックを、各マップ install_dir 指定で再生成する。

    元ファイルは .yaml.bak にバックアップ。launch_args等は entries の値をそのまま使う。
    """
    path = Path(config_path)
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if l.startswith("ark_hosts:")), None)
    if start is None:
        raise RuntimeError("config.yaml に ark_hosts: が見つかりません")
    end = start + 1
    while end < len(lines) and (lines[end][:1] in (" ", "\t") or lines[end].strip() == ""):
        end += 1                              # インデント行/空行が続く間=ark_hostsブロック
    new_lines = lines[:start] + _gen_block(entries) + [""] + lines[end:]
    path.with_suffix(".yaml.bak").write_text(text, encoding="utf-8")
    path.write_text("\n".join(new_lines), encoding="utf-8")
