"""Minecraft(Fabric)サーバーの既存ワールドを別バージョンへ変更する。

方針(ユーザー合意):
  - **アップグレードのみ**。ダウングレードは原則禁止(世界データが壊れるため)。
  - mod入りは互換性が切れることを警告し、対応版があるmodはModrinthで自動更新する。
    対応版が無いmodはそのまま残す(ユーザーが「そのまま進む」を選んだ前提)。
  - 変更前に必ずワールドをバックアップする。ワールドは新版起動時に自動アップグレードされる。

Fabricの導入方法は provisioners/minecraft_fabric.yaml と同じ(Fabric Meta の
loader/<GAME>/<LOADER>/<INSTALLER>/server/jar から fabric-server-launch.jar を取得)。
"""
from __future__ import annotations

import json
import urllib.request

from core import modmanager, onlinemods

FABRIC_META = "https://meta.fabricmc.net"


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "game-server-manager"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _sudo(client, password: str, script: str, timeout: int = 600) -> str:
    """sudoでスクリプトを実行して出力を返す(modmanager._sudo_run は MODMGR_OK 前提の
    別用途なので、バージョン変更用に判定なしの実行ヘルパを持つ)。"""
    sftp = client.open_sftp()
    with sftp.open("/tmp/gsm_mcversion.sh", "w") as f:
        f.write(script)
    sftp.close()
    stdin, stdout, _ = client.exec_command(
        "sudo -S bash /tmp/gsm_mcversion.sh 2>&1", timeout=timeout)
    stdin.write(password + "\n")
    stdin.flush()
    return stdout.read().decode("utf-8", "replace")


# ---------------- バージョン一覧 ----------------
def game_versions() -> list[dict]:
    """Fabric公式が対応するMCバージョン(新しい順)。[{version, stable}]。"""
    return _get_json(f"{FABRIC_META}/v2/versions/game")


def _stable_latest(kind: str) -> str:
    data = _get_json(f"{FABRIC_META}/v2/versions/{kind}")
    return next((v["version"] for v in data if v.get("stable")), data[0]["version"])


def java_for(version: str) -> int:
    """provisionerと同じ規則で必要Javaメジャーを返す。"""
    parts = version.split(".")
    try:
        maj = int(parts[0])
    except ValueError:
        return 21
    if maj == 1:
        mn = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        pt = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if mn <= 16:
            return 8
        if mn <= 20:
            return 21 if (mn == 20 and pt >= 5) else 17
        return 21
    return 21  # 26.x 以降


def upgradable_versions(current: str, stable_only: bool = True) -> list[str]:
    """current より新しいバージョンだけを新しい順で返す(ダウングレード除外)。

    Fabric Meta の game 一覧は新しい順。current の位置より前(=新しい)を返す。
    """
    versions = game_versions()
    order = [v["version"] for v in versions]
    stable = {v["version"] for v in versions if v.get("stable")}
    if current not in order:
        # 現行版が一覧に無い(スナップショット等)場合は安全側で全stableを返す
        cands = order
    else:
        cands = order[: order.index(current)]        # current より新しいものだけ
    return [v for v in cands if (v in stable or not stable_only)]


def is_upgrade(current: str, target: str) -> bool:
    """target が current より新しければ True。判定不能や同一/古いは False。"""
    order = [v["version"] for v in game_versions()]
    if current not in order or target not in order:
        return False
    return order.index(target) < order.index(current)


# ---------------- 現行バージョン検出 ----------------
def installed_version(profile) -> str | None:
    """install_dir/versions/ の最新フォルダ名を現行バージョンとみなす。"""
    c = modmanager._connect(profile)
    try:
        _, out, _ = c.exec_command(
            f"ls -1t '{profile.install_dir}/versions' 2>/dev/null | head -1", timeout=30)
        name = out.read().decode("utf-8", "replace").strip()
    finally:
        c.close()
    return name or None


# ---------------- mod互換プラン ----------------
def mod_plan(profile, target: str, progress=lambda t: None) -> list[dict]:
    """導入済み各modについて、目標版で更新可能か判定する。

    戻り値: [{file, name, current, status, new_entry}]
      status = 'update'      … 目標版に対応版あり(new_entry に導入情報)
             | 'incompatible' … Modrinth登録だが目標版なし(互換切れ)
             | 'unknown'      … Modrinth未登録(CF等)で判定不可
    fabric-api 等の環境ライブラリも普通に対象になる(更新される)。
    """
    import hashlib  # noqa: F401 (SHA1はサーバー側で計算)
    import urllib.error

    client = modmanager._connect(profile)
    try:
        _, stdout, _ = client.exec_command(
            f"sha1sum '{profile.mods_dir}'/*.jar 2>/dev/null", timeout=60)
        raw = stdout.read().decode("utf-8", "replace")
    finally:
        client.close()

    hashes = {}                                   # sha1 -> filename
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            hashes[parts[0]] = parts[-1].rsplit("/", 1)[-1]

    plan = []
    for sha1, fname in hashes.items():
        rec = {"file": fname, "name": fname, "current": "?",
               "status": "unknown", "new_entry": None}
        try:
            ver = _get_json(
                f"https://api.modrinth.com/v2/version_file/{sha1}?algorithm=sha1")
            rec["current"] = ver.get("version_number", "?")
            pid = ver.get("project_id")
            try:
                latest = onlinemods.resolve_modrinth(pid, target)   # 目標版の最新
                rec["name"] = latest.get("name", fname)
                rec["status"] = "update"
                rec["new_entry"] = latest               # url/filename/name/version を含む
            except onlinemods.ModSearchError:
                rec["status"] = "incompatible"          # 登録済みだが目標版なし
                rec["name"] = fname
        except urllib.error.HTTPError:
            rec["status"] = "unknown"                    # Modrinth未登録(CF等)
        except Exception:
            rec["status"] = "unknown"
        plan.append(rec)
        progress(f"互換確認: {rec['name']}")
    return plan


# ---------------- 実行 ----------------
def _server_side_upgrade(profile, target: str, progress) -> dict:
    """停止 → fabric-server-launch.jar 差し替え → Java/systemd 更新(sudo一括)。"""
    loader = _stable_latest("loader")
    installer = _stable_latest("installer")
    need_java = java_for(target)
    url = f"{FABRIC_META}/v2/versions/loader/{target}/{loader}/{installer}/server/jar"
    install = profile.install_dir
    service = profile.service
    ru = profile.runtime_user
    script = f"""
set -e
cd '{install}'
systemctl stop '{service}' 2>/dev/null || true
curl -fsSL -o fabric-server-launch.jar.new "{url}"
mv -f fabric-server-launch.jar.new fabric-server-launch.jar
NEED={need_java}
JBIN=$(ls -d /usr/lib/jvm/temurin-${{NEED}}-jre*/bin/java 2>/dev/null | head -1)
if [ -z "$JBIN" ]; then
  apt-get install -y -qq "temurin-${{NEED}}-jre" >/dev/null 2>&1 || true
  JBIN=$(ls -d /usr/lib/jvm/temurin-${{NEED}}-jre*/bin/java 2>/dev/null | head -1)
fi
[ -x "$JBIN" ] || JBIN=$(command -v java)
sed -i -E "s#^ExecStart=[^ ]+#ExecStart=$JBIN#" '/etc/systemd/system/{service}.service'
systemctl daemon-reload
chown -R {ru}:{ru} '{install}'
echo "UPGRADE_OK JAVA=$JBIN LOADER={loader} INSTALLER={installer}"
"""
    progress(f"Fabric再導入(MC {target} / Java {need_java})…")
    c = modmanager._connect(profile)
    try:
        out = _sudo(c, profile.ssh_password, script)
    finally:
        c.close()
    if "UPGRADE_OK" not in out:
        raise RuntimeError(f"サーバー側の更新に失敗しました:\n{out[-800:]}")
    return {"loader": loader, "installer": installer, "java": need_java}


def _start(profile, progress) -> None:
    progress("起動中…")
    c = modmanager._connect(profile)
    try:
        _sudo(c, profile.ssh_password, f"systemctl start '{profile.service}'")
    finally:
        c.close()


def change_version(profile, target: str, plan: list[dict], backup_cfg,
                   progress=lambda t: None) -> dict:
    """バージョン変更本体。plan は mod_plan() の結果(ユーザー確認済み)。

    - updatable な mod は目標版へ自動更新。incompatible/unknown はそのまま残す。
    - 変更前にワールドをバックアップ。
    呼び出し側(routes)がダウングレードでないことを保証すること。
    """
    from core import backup as backup_mod

    progress("ワールドをバックアップ中…")
    backup_path = backup_mod.mc_backup(profile, backup_cfg, progress=progress)

    info = _server_side_upgrade(profile, target, progress)

    updatable = [m for m in (plan or []) if m.get("status") == "update" and m.get("new_entry")]
    left = [m["name"] for m in (plan or []) if m.get("status") != "update"]
    if updatable:
        progress(f"mod を目標版へ更新中… ({len(updatable)}件)")
        old_files = [m["file"] for m in updatable]
        entries = [m["new_entry"] for m in updatable]
        modmanager.remove_mods(profile, old_files, restart=False)
        modmanager.install_online(profile, entries, restart=False, progress=progress)

    _start(profile, progress)
    return {
        "target": target,
        "backup": backup_path,
        "updated_mods": [m["name"] for m in updatable],
        "left_mods": left,
        **info,
    }
