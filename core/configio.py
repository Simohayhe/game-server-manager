"""GSMの設定のエクスポート/インポート(独自の設定ファイル作成)。

対象は config.yaml(本体設定)＋各種状態json(通知・予約・ポート開放など)。これらを
1つのアーカイブ(zip)にまとめ、任意でパスワード暗号化して1ファイルにする。

- with_secrets=True  : パスワード等の機密も含める(完全復元・別PC移行向け)。暗号化推奨。
- with_secrets=False : パスワード等を伏せてから書き出す(共有向け=サニタイズ)。
- password あり       : アーカイブ全体を暗号化(Fernet + PBKDF2)。インポート時に同じ
                        パスワードを入力すれば復号できる(間違えば復号失敗=弾く)。

暗号化ファイルの先頭は識別子 "GSMENC1" で始まる。インポート側はこれを見て自動判別する。
"""
from __future__ import annotations

import base64
import datetime as _dt
import io
import json
import os
import zipfile
from pathlib import Path

from ruamel.yaml import YAML

# エクスポート対象。config.yaml は必須、残りは存在すれば含める(状態系)。
SETTINGS_FILES = [
    "config.yaml",        # 本体設定(サーバー/ネットワーク/DNS/バックアップ等)
    "notify.json",        # Discord通知
    "schedules.json",     # 再起動/バックアップ予約
    "portsync.json",      # 自動ポート開放のON/OFF
    "crashwatch.json",    # クラッシュ自動復旧のON/OFF
    "arkbehavior.json",   # ARKの挙動(リスポーン等)
    "arkevent.json",      # ARKイベント
    "dynconfig.json",     # ARK dynamic config
    "clusters.json",      # MCクラスタ
]
# config.yaml 内でサニタイズ(伏字)するキー名(大文字小文字無視・部分一致)。
SECRET_KEY_HINTS = ("password", "api_key", "secret", "token", "webhook")
MAGIC = b"GSMENC1\n"


class ConfigIOError(Exception):
    pass


# ---------------------------------------------------------------------------
# 暗号化(パスワード → 鍵導出 → Fernet)
# ---------------------------------------------------------------------------
def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def encrypt(data: bytes, password: str) -> bytes:
    from cryptography.fernet import Fernet
    salt = os.urandom(16)
    token = Fernet(_derive_key(password, salt)).encrypt(data)
    return MAGIC + salt + token


def is_encrypted(blob: bytes) -> bool:
    return blob[:len(MAGIC)] == MAGIC


def decrypt(blob: bytes, password: str) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken
    if not is_encrypted(blob):
        raise ConfigIOError("暗号化ファイルではありません。")
    salt = blob[len(MAGIC):len(MAGIC) + 16]
    token = blob[len(MAGIC) + 16:]
    try:
        return Fernet(_derive_key(password, salt)).decrypt(token)
    except InvalidToken:
        raise ConfigIOError("パスワードが違います(または壊れたファイルです)。")


# ---------------------------------------------------------------------------
# サニタイズ(機密を伏字にする)
# ---------------------------------------------------------------------------
def _is_secret_key(key: str) -> bool:
    k = str(key).lower()
    return any(h in k for h in SECRET_KEY_HINTS)


def _sanitize_obj(obj):
    """dict/list を再帰的に走査し、機密キーの値を空文字にする(構造は保つ)。"""
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if _is_secret_key(k) and isinstance(obj[k], (str, int, float)):
                obj[k] = ""
            else:
                _sanitize_obj(obj[k])
    elif isinstance(obj, list):
        for v in obj:
            _sanitize_obj(v)
    return obj


def _sanitize_yaml(text: str) -> str:
    yaml = YAML()                     # コメント保持のまま伏字化
    yaml.preserve_quotes = True
    data = yaml.load(text)
    _sanitize_obj(data)
    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


def _sanitize_json(text: str) -> str:
    data = json.loads(text)
    return json.dumps(_sanitize_obj(data), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# エクスポート
# ---------------------------------------------------------------------------
def _build_zip(app_dir: Path, with_secrets: bool, app_version: str = "") -> bytes:
    files_written = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in SETTINGS_FILES:
            fp = app_dir / name
            if not fp.exists():
                continue
            text = fp.read_text(encoding="utf-8")
            if not with_secrets:
                try:
                    if name.endswith(".yaml"):
                        text = _sanitize_yaml(text)
                    elif name.endswith(".json"):
                        text = _sanitize_json(text)
                except Exception:      # noqa: BLE001  伏字化に失敗したら安全側=そのファイルは入れない
                    continue
            z.writestr(name, text)
            files_written.append(name)
        manifest = {
            "kind": "gsm-settings",
            "version": 1,
            "app_version": app_version,
            "with_secrets": with_secrets,
            "created": _dt.datetime.now().isoformat(timespec="seconds"),
            "files": files_written,
        }
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()


def export_bundle(app_dir: str | Path, with_secrets: bool = True,
                  password: str = "", app_version: str = "") -> tuple[bytes, str]:
    """設定アーカイブを作る。戻り値: (データ, 推奨ファイル名)。

    password を指定すると暗号化する(拡張子 .gsmenc)。無指定なら平文zip(.gsmbackup)。
    """
    app_dir = Path(app_dir)
    if not (app_dir / "config.yaml").exists():
        raise ConfigIOError(f"config.yaml が見つかりません: {app_dir}")
    raw = _build_zip(app_dir, with_secrets, app_version)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if password:
        return encrypt(raw, password), f"gsm-settings_{ts}.gsmenc"
    tag = "" if with_secrets else "_safe"
    return raw, f"gsm-settings_{ts}{tag}.gsmbackup"


# ---------------------------------------------------------------------------
# インポート
# ---------------------------------------------------------------------------
def peek(blob: bytes, password: str = "") -> dict:
    """アーカイブの中身(manifest)を覗く。暗号化なら password 必須。"""
    data = decrypt(blob, password) if is_encrypted(blob) else blob
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "manifest.json" in z.namelist():
                return json.loads(z.read("manifest.json"))
            return {"files": [n for n in z.namelist()]}
    except zipfile.BadZipFile:
        raise ConfigIOError("設定ファイルとして読み込めません(壊れているか形式が違います)。")


def import_bundle(app_dir: str | Path, blob: bytes, password: str = "",
                  progress=lambda t: None) -> dict:
    """アーカイブを app_dir へ展開する。展開前に現在の設定を自動バックアップする。

    config.yaml は load_config で検証してから書き込む(壊れた設定で上書きしない)。
    戻り値: {"imported": [...], "backup": <退避先>, "with_secrets": bool}
    """
    from core.config import load_config
    app_dir = Path(app_dir)
    data = decrypt(blob, password) if is_encrypted(blob) else blob
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ConfigIOError("設定ファイルとして読み込めません(壊れているか形式が違います)。")

    names = [n for n in zf.namelist() if n in SETTINGS_FILES]
    if "config.yaml" not in names:
        raise ConfigIOError("アーカイブに config.yaml が含まれていません。")

    # config.yaml を一時ファイルで検証(壊れた設定なら中止=現状を守る)
    progress("設定を検証中…")
    tmp = app_dir / ".import_config_check.yaml"
    tmp.write_bytes(zf.read("config.yaml"))
    try:
        load_config(tmp)
    except Exception as exc:                       # noqa: BLE001
        raise ConfigIOError(f"取り込む config.yaml が不正です: {exc}")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    # 現在の設定を退避(取り込み前の状態にいつでも戻せるように)
    progress("現在の設定をバックアップ中…")
    backup_path = _backup_current(app_dir)

    imported = []
    for name in names:
        (app_dir / name).write_bytes(zf.read(name))
        imported.append(name)
        progress(f"取り込み: {name}")

    manifest = {}
    if "manifest.json" in zf.namelist():
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except ValueError:
            pass
    return {"imported": imported, "backup": str(backup_path),
            "with_secrets": bool(manifest.get("with_secrets", True))}


def _backup_current(app_dir: Path) -> Path:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = app_dir / f"settings-pre-import_{ts}.gsmbackup"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in SETTINGS_FILES:
            fp = app_dir / name
            if fp.exists():
                z.writestr(name, fp.read_text(encoding="utf-8"))
    dest.write_bytes(buf.getvalue())
    return dest
