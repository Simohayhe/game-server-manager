"""ARK(ASA)の .arkprofile / .arktribe から人間が読める情報を取り出す(標準ライブラリのみ)。

.arkprofile は UE の serialize 形式。完全パースはせず、必要なフィールド
(PlayerName=アカウント名 / PlayerCharacterName=キャラ名 / PlayerDataID / TribeID / レベル)
だけを FString 単位で拾う。ファイル名(拡張子除く)= EOS/PlatformID で RCON ListPlayers と突合できる。

FString の格納:
  int32 len ; len>=0 → ASCII(len は末尾NUL込みのバイト数) / len<0 → UTF-16LE((-len) は文字数, 末尾NUL込み)
"""
from __future__ import annotations

import struct
from pathlib import Path


def _read_fstring(buf: bytes, pos: int) -> tuple[str | None, int]:
    """pos の FString を読み、(文字列, 次のpos) を返す。読めなければ (None, pos)。"""
    if pos < 0 or pos + 4 > len(buf):
        return None, pos
    n = struct.unpack_from("<i", buf, pos)[0]
    pos += 4
    if n == 0:
        return "", pos
    if n > 0:
        if n > 4096 or pos + n > len(buf):
            return None, pos
        raw = buf[pos:pos + n]
        pos += n
        try:
            return raw.split(b"\x00", 1)[0].decode("utf-8", "replace"), pos
        except Exception:                                      # noqa: BLE001
            return None, pos
    # UTF-16LE
    cnt = -n
    if cnt > 4096 or pos + cnt * 2 > len(buf):
        return None, pos
    raw = buf[pos:pos + cnt * 2]
    pos += cnt * 2
    try:
        return raw.decode("utf-16-le", "replace").split("\x00", 1)[0], pos
    except Exception:                                          # noqa: BLE001
        return None, pos


# ARKのプロパティ: <FString name><FString type><int32 arrayIndex=0><int32 size><byte guid=0><value>
# → 値は type FString 終端の +9 バイト後(4+4+1)。まず9、ずれ対策で近傍も試す。
_VAL_SKIPS = (9, 10, 8, 11, 7, 13, 5)


def _name_matches(buf: bytes, idx: int, key: bytes):
    """buf[idx:] が key の FString 値なら (次pos, type FString, type後pos) を返す。"""
    name, p = _read_fstring(buf, idx - 4)          # key の長さ4バイト前が name の先頭
    if name and name.encode("utf-8", "replace") == key.rstrip(b"\x00"):
        typ, p2 = _read_fstring(buf, p)
        if typ and "Property" in typ:
            return p2, typ
    return None, None


def _value_after(buf: bytes, key: bytes):
    """key(例 b'PlayerName')の StrProperty 値(文字列)を返す。"""
    idx = buf.find(key)
    while idx != -1:
        p2, typ = _name_matches(buf, idx, key)
        if p2 is not None:
            for skip in _VAL_SKIPS:
                val, _ = _read_fstring(buf, p2 + skip)
                if val is not None and val != "" and "Property" not in val:
                    return val
        idx = buf.find(key, idx + 1)
    return None


def _int_after(buf: bytes, key: bytes, prop: bytes):
    """key の数値プロパティ値を返す(int32=既定, UInt16Propertyのみ2バイト)。"""
    fmt = "<H" if prop == b"UInt16Property" else "<i"
    idx = buf.find(key)
    while idx != -1:
        p2, typ = _name_matches(buf, idx, key)
        if p2 is not None and typ and typ.startswith(prop.decode()):
            for skip in (9, 10, 8):
                try:
                    return struct.unpack_from(fmt, buf, p2 + skip)[0]
                except struct.error:
                    pass
        idx = buf.find(key, idx + 1)
    return None


def parse_bytes(buf: bytes, player_id: str) -> dict:
    """.arkprofile のバイト列から表示用情報を抽出(zip内読取用)。"""
    out = {
        "player_id": player_id,        # ファイル名(拡張子除く) = EOS/PlatformID
        "account_name": _value_after(buf, b"PlayerName"),          # Steam/EOS表示名
        "character_name": _value_after(buf, b"PlayerCharacterName"),  # ゲーム内キャラ名
        "tribe_id": _int_after(buf, b"TribeID", b"IntProperty"),
        "level": None,
        "error": None,
    }
    lvl = _int_after(buf, b"CharacterStatusComponent_ExtraCharacterLevel", b"UInt16Property")
    if lvl is not None:
        out["level"] = lvl + 1         # ExtraLevel は 1 起点表示にする慣習
    return out


def parse_profile(path: str | Path) -> dict:
    """.arkprofile から表示用情報を抽出。失敗しても例外は投げず取れた分だけ返す。"""
    path = Path(path)
    try:
        buf = path.read_bytes()
    except OSError as e:
        return {"player_id": path.stem, "account_name": None, "character_name": None,
                "tribe_id": None, "level": None, "error": str(e)}
    return parse_bytes(buf, path.stem)


def label(prof: dict) -> str:
    """一覧表示用の短いラベル。"""
    acc = prof.get("account_name") or "?"
    ch = prof.get("character_name")
    lv = prof.get("level")
    s = acc
    if ch and ch != acc:
        s += f" ({ch})"
    if lv:
        s += f" Lv{lv}"
    return s
