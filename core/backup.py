"""ゲームサーバーのバックアップ/復元(zip圧縮・世代管理・パス指定)。

- ARK(ホスト): ShooterGame/Saved フォルダをローカルでzip(全マップのワールド+設定)。
- Minecraft(VM): ワールド等をSSHでtar→ダウンロードして保存。
保存先: <backup.path>/<target>/<target>_<timestamp>.(zip|tar.gz)。
世代管理: 新しい keep 個を残し、それより古いものを自動削除。
復元は既存データを上書きするので、呼び出し側でサーバー停止+確認を行うこと。
"""
from __future__ import annotations

import datetime as _dt
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

import paramiko


@dataclass
class BackupConfig:
    path: str = r"C:\GameBackups"
    keep: int = 10               # ワールド等(大容量)の世代数
    compress: bool = True
    players_keep: int = 60       # プレイヤーデータ(極小)の世代数。keepとは別に多く残す


class BackupError(Exception):
    pass


def _ts() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _target_dir(cfg: BackupConfig, target: str) -> Path:
    d = Path(cfg.path) / target
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune(target_dir: Path, prefix: str, keep: int,
           progress=lambda t: None) -> list[str]:
    files = sorted(target_dir.glob(f"{prefix}_*"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in files[keep:]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    if removed:
        progress(f"古い世代を削除: {len(removed)}件")
    return removed


def list_backups(cfg: BackupConfig, target: str) -> list[dict]:
    d = Path(cfg.path) / target
    if not d.exists():
        return []
    out = []
    for f in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.is_file() and (f.suffix == ".zip" or f.name.endswith(".tar.gz")):
            st = f.stat()
            out.append({
                "path": str(f), "name": f.name,
                "size_mb": round(st.st_size / 1_000_000, 1),
                "mtime": _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "epoch": st.st_mtime,     # 相対時刻(◯分前)表示に使う
            })
    return out


def ark_saved_dir(config_dir: str) -> Path:
    """ark_host.config_dir(...\\Saved\\Config\\WindowsServer)から Saved フォルダを求める。"""
    return Path(config_dir).parent.parent


# ---------------------------------------------------------------------------
# ARK(ホスト・ローカル)。マップごとに、そのマップのセーブフォルダだけをzipする。
#   保存先: <path>/ARK/<map_label>/<map_label>_<ts>.zip
#   zip内はセーブフォルダ名(save_subdir)を先頭に含むので、Savedへ展開すれば元位置に戻る。
# ---------------------------------------------------------------------------
def _zip_subdir(root: Path, subdir: str, dest: Path) -> int:
    """root/subdir 配下を、arcnameに subdir/ を含めてzipする(復元で元位置に戻すため)。"""
    base = root / subdir
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for r, _dirs, files in os.walk(base):
            for name in files:
                fp = Path(r) / name
                try:
                    z.write(fp, fp.relative_to(root))  # 例: Genesis1/Genesis_WP.ark
                    n += 1
                except (OSError, ValueError):
                    pass  # 使用中/読めないファイルはスキップ
    return n


def ark_backup(saved_root: str | Path, cfg: BackupConfig,
               map_label: str, save_subdir: str,
               progress=lambda t: None) -> str:
    """1マップ分のセーブ(Saved/<save_subdir>)をバックアップする。"""
    root = Path(saved_root)
    src = root / save_subdir
    if not src.exists():
        raise BackupError(
            f"マップのセーブフォルダが見つかりません: {src}\n"
            "(一度もこのマップを起動・保存していない可能性があります)")
    d = _target_dir(cfg, f"ARK/{map_label}")     # <path>/ARK/<map_label>
    dest = d / f"{map_label}_{_ts()}.zip"
    progress(f"{map_label}: セーブを圧縮中…(Saved/{save_subdir})")
    n = _zip_subdir(root, save_subdir, dest)
    _prune(d, map_label, cfg.keep, progress)     # このマップだけで世代管理
    progress(f"{map_label}: 完了 {dest.name}({n}ファイル)")
    return str(dest)


PLAYER_PATTERNS = ("*.arkprofile", "*.arktribe")


def ark_player_backup(entries, cluster_dir: str | Path | None, cfg: BackupConfig,
                      keep: int | None = None, progress=lambda t: None) -> str:
    """全マップのプレイヤーデータ(.arkprofile/.arktribe)＋クラスタ転送データだけを1zipにする。

    世界セーブ(.ark)を含めないので合計1MB弱＝非常に軽く、saveworldもしないので
    稼働中マップに影響しない(ディスク上の最後のセーブ時点のプレイヤーデータを写す)。
    entries: [(map_label, saved_root, save_subdir), ...]
    zip内は <map_label>/<Savedからの相対パス> なので、どのマップの誰かが分かる。
    保存先: <path>/ARK/_players/players_<ts>.zip
    """
    d = _target_dir(cfg, "ARK/_players")
    dest = d / f"players_{_ts()}.zip"
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for label, saved_root, save_subdir in entries:
            root = Path(saved_root)
            base = root / save_subdir
            if not base.exists():
                continue
            for pat in PLAYER_PATTERNS:
                for fp in base.rglob(pat):
                    try:
                        z.write(fp, f"{label}/{fp.relative_to(root)}")
                        n += 1
                    except (OSError, ValueError):
                        pass          # 使用中/読めないファイルはスキップ
        cl = Path(cluster_dir) if cluster_dir else None
        if cl and cl.exists():        # クラスタ転送中のキャラ/アイテムも保護対象
            for fp in cl.rglob("*"):
                if fp.is_file():
                    try:
                        z.write(fp, f"_cluster/{fp.relative_to(cl)}")
                        n += 1
                    except (OSError, ValueError):
                        pass
    # プレイヤーBKは専用の保持数(players_keep)で剪定する。ワールド用の keep(既定10)を
    # 使うと、手動BKや復元前の安全BKで大量に消えてしまう(実際にそれで50件消えた)。
    _prune(d, "players", cfg.players_keep if keep is None else keep, progress)
    progress(f"プレイヤーデータ: 完了 {dest.name}({n}ファイル)")
    return str(dest)


def ark_players_in_backup(backup_file: str | Path) -> list[dict]:
    """プレイヤーデータBK(players_*.zip)の中身を一覧化する。

    各 .arkprofile をzipから読んで解析し、誰のデータかを人間可読で返す。
    戻り: [{entry, player_id, map_label, account_name, character_name, level, tribe_id}, ...]
    """
    from core import arkprofile
    bf = Path(backup_file)
    out: list[dict] = []
    if not bf.exists():
        raise BackupError(f"バックアップが見つかりません: {bf}")
    with zipfile.ZipFile(bf) as z:
        for name in z.namelist():
            if not name.lower().endswith(".arkprofile"):
                continue
            parts = name.split("/")
            map_label = parts[0] if parts else "?"
            pid = Path(name).stem
            try:
                info = arkprofile.parse_bytes(z.read(name), pid)
            except Exception:                                  # noqa: BLE001
                info = {"player_id": pid, "account_name": None,
                        "character_name": None, "tribe_id": None, "level": None}
            info["entry"] = name
            info["map_label"] = map_label
            out.append(info)
    # 表示名でソート(名前不明は後ろ)
    out.sort(key=lambda d: ((d.get("account_name") or "￿").lower(),
                            d.get("map_label") or ""))
    return out


def ark_player_restore(backup_file: str | Path, label_to_root: dict,
                       cluster_dir: str | Path | None,
                       entries: list[str] | None = None,
                       progress=lambda t: None) -> int:
    """プレイヤーデータBKから指定エントリ(なければ全部)を元の場所へ復元する。

    label_to_root: {map_label: saved_root}。zip内 "<label>/<rel>" を saved_root/<rel> へ。
    "_cluster/<rel>" は cluster_dir/<rel> へ。復元したファイル数を返す。
    ※ 上書き。呼び出し側で対象マップ停止＋事前の安全バックアップを行うこと。
    """
    bf = Path(backup_file)
    if not bf.exists():
        raise BackupError(f"バックアップが見つかりません: {bf}")
    want = set(entries) if entries is not None else None
    n = 0
    with zipfile.ZipFile(bf) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            if want is not None and name not in want:
                continue
            parts = name.split("/", 1)
            if len(parts) != 2:
                continue
            head, rel = parts
            if head == "_cluster":
                if not cluster_dir:
                    continue
                dst = Path(cluster_dir) / rel
            else:
                root = label_to_root.get(head)
                if not root:
                    progress(f"⚠ マップ '{head}' の保存先が不明なのでスキップ")
                    continue
                dst = Path(root) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(dst, "wb") as f:
                f.write(src.read())
            n += 1
    progress(f"復元完了: {n} ファイル")
    return n


def ark_restore(backup_file: str, saved_root: str | Path,
                progress=lambda t: None) -> None:
    """マップ単位のバックアップをSavedへ展開する(zip内の save_subdir/ を元位置へ)。"""
    bf = Path(backup_file)
    if not bf.exists():
        raise BackupError(f"バックアップが見つかりません: {bf}")
    dst = Path(saved_root)
    dst.mkdir(parents=True, exist_ok=True)
    progress("復元中…(このマップのセーブをSavedへ展開)")
    with zipfile.ZipFile(bf) as z:
        z.extractall(dst)
    progress("復元完了")


# ---------------------------------------------------------------------------
# Minecraft(VM・SSH)
# ---------------------------------------------------------------------------
def _ssh(profile):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(profile.address, username=profile.ssh_user,
                  password=profile.ssh_password, port=profile.ssh_port, timeout=15)
    except Exception as exc:
        raise BackupError(f"{profile.address} にSSH接続できません: {exc}") from exc
    return c


def mc_backup(profile, cfg: BackupConfig, world: str = "world",
              progress=lambda t: None) -> str:
    d = _target_dir(cfg, profile.name)
    dest = d / f"{profile.name}_{_ts()}.tar.gz"
    client = _ssh(profile)
    try:
        install = profile.install_dir
        remote = "/tmp/gsm_mc_backup.tar.gz"
        progress(f"{profile.display_name}: VM上でワールドをtar圧縮中…")
        # ワールド+設定に加え、存在すれば mods/ と config/ も含める(mod鯖の完全復元用)。
        cmd = (f"cd '{install}' && FILES='server.properties eula.txt {world}'; "
               f"for d in mods config; do [ -e \"$d\" ] && FILES=\"$FILES $d\"; done; "
               f"tar czf {remote} $FILES 2>/dev/null; echo DONE")
        _, o, _ = client.exec_command(cmd, timeout=900)
        o.read()
        progress(f"{profile.display_name}: ダウンロード中…")
        sftp = client.open_sftp()
        sftp.get(remote, str(dest))
        try:
            sftp.remove(remote)
        except IOError:
            pass
        sftp.close()
    finally:
        client.close()
    _prune(d, profile.name, cfg.keep, progress)
    progress(f"{profile.display_name}: 完了 {dest.name}")
    return str(dest)


def mc_restore(profile, backup_file: str, world: str = "world",
               progress=lambda t: None) -> None:
    if not Path(backup_file).exists():
        raise BackupError(f"バックアップが見つかりません: {backup_file}")
    client = _ssh(profile)
    try:
        install = profile.install_dir
        remote = "/tmp/gsm_mc_restore.tar.gz"
        progress(f"{profile.display_name}: アップロード中…")
        sftp = client.open_sftp()
        sftp.put(backup_file, remote)
        with sftp.open("/tmp/gsm_mc_restore.sh", "w") as f:
            f.write(f"""#!/bin/bash
set -e
cd '{install}'
rm -rf '{world}'
tar xzf {remote}
chown -R {profile.runtime_user}:{profile.runtime_user} '{install}'
rm -f {remote}
echo RESTORE_OK
""")
        sftp.close()
        progress(f"{profile.display_name}: 展開中…(既存ワールドを置換)")
        stdin, stdout, _ = client.exec_command(
            "sudo -S bash /tmp/gsm_mc_restore.sh 2>&1", timeout=600)
        stdin.write(profile.ssh_password + "\n")
        stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        if "RESTORE_OK" not in out:
            raise BackupError(f"復元に失敗しました:\n{out[-500:]}")
    finally:
        client.close()
    progress("復元完了")


# ---------------------------------------------------------------------------
# Palworld(VM・SSH)。セーブ一式(Pal/Saved)をtar.gz。
# ---------------------------------------------------------------------------
def pal_backup(profile, cfg: BackupConfig, progress=lambda t: None) -> str:
    d = _target_dir(cfg, profile.name)
    dest = d / f"{profile.name}_{_ts()}.tar.gz"
    client = _ssh(profile)
    try:
        install = profile.install_dir
        remote = "/tmp/gsm_pal_backup.tar.gz"
        progress(f"{profile.display_name}: VM上でセーブをtar圧縮中…")
        cmd = (f"cd '{install}' && tar czf {remote} Pal/Saved 2>/dev/null; echo DONE")
        _, o, _ = client.exec_command(cmd, timeout=900)
        o.read()
        progress(f"{profile.display_name}: ダウンロード中…")
        sftp = client.open_sftp()
        sftp.get(remote, str(dest))
        try:
            sftp.remove(remote)
        except IOError:
            pass
        sftp.close()
    finally:
        client.close()
    _prune(d, profile.name, cfg.keep, progress)
    progress(f"{profile.display_name}: 完了 {dest.name}")
    return str(dest)


def pal_restore(profile, backup_file: str, progress=lambda t: None) -> None:
    if not Path(backup_file).exists():
        raise BackupError(f"バックアップが見つかりません: {backup_file}")
    client = _ssh(profile)
    try:
        install = profile.install_dir
        remote = "/tmp/gsm_pal_restore.tar.gz"
        owner = profile.ssh_user            # Palworldはssh_user所有(例 master)
        progress(f"{profile.display_name}: アップロード中…")
        sftp = client.open_sftp()
        sftp.put(backup_file, remote)
        with sftp.open("/tmp/gsm_pal_restore.sh", "w") as f:
            f.write(f"""#!/bin/bash
set -e
cd '{install}'
rm -rf 'Pal/Saved'
tar xzf {remote}
chown -R {owner}:{owner} '{install}/Pal/Saved'
rm -f {remote}
echo RESTORE_OK
""")
        sftp.close()
        progress(f"{profile.display_name}: 展開中…(既存セーブを置換)")
        stdin, stdout, _ = client.exec_command(
            "sudo -S bash /tmp/gsm_pal_restore.sh 2>&1", timeout=600)
        stdin.write(profile.ssh_password + "\n")
        stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        if "RESTORE_OK" not in out:
            raise BackupError(f"復元に失敗しました:\n{out[-500:]}")
    finally:
        client.close()
    progress("復元完了")
