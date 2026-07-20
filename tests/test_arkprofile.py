"""ARK .arkprofile パーサの回帰テスト。

2026-07-21のプレイヤー復元機能で、値が「type FString 終端の +9バイト後」であることを突き止めた。
合成バイト列で PlayerName / PlayerCharacterName(UTF-16) / TribeID / レベル の抽出を固定する。
実サーバーが手元にある時だけ実データ照合も行う(無ければスキップ)。
"""
import glob
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import arkprofile  # noqa: E402


def _fstr(s: str) -> bytes:
    b = s.encode("utf-8") + b"\x00"
    return struct.pack("<i", len(b)) + b


def _fstr16(s: str) -> bytes:
    b = s.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<i", -(len(s) + 1)) + b


def _prop(name: str, typ: str, value: bytes) -> bytes:
    # <FString name><FString type><int32 idx=0><int32 size><byte guid=0><value>
    return (_fstr(name) + _fstr(typ) + struct.pack("<i", 0)
            + struct.pack("<i", len(value)) + b"\x00" + value)


def _synthetic() -> bytes:
    buf = b"HEADERJUNK"
    buf += _prop("PlayerName", "StrProperty", _fstr("TestGuy"))
    buf += _prop("PlayerCharacterName", "StrProperty", _fstr16("サバイバー"))
    buf += _prop("TribeID", "IntProperty", struct.pack("<i", 1234567))
    buf += _prop("CharacterStatusComponent_ExtraCharacterLevel",
                 "UInt16Property", struct.pack("<H", 42))
    return buf


def test_parse_playername_ascii():
    p = arkprofile.parse_bytes(_synthetic(), "someid")
    assert p["account_name"] == "TestGuy", p["account_name"]


def test_parse_character_name_utf16_japanese():
    p = arkprofile.parse_bytes(_synthetic(), "someid")
    assert p["character_name"] == "サバイバー", p["character_name"]


def test_parse_tribe_and_level():
    p = arkprofile.parse_bytes(_synthetic(), "someid")
    assert p["tribe_id"] == 1234567, p["tribe_id"]
    assert p["level"] == 43, p["level"]        # ExtraLevel 42 → 表示43


def test_player_id_from_filename():
    p = arkprofile.parse_profile(Path("/x/0002abc123.arkprofile"))
    assert p["player_id"] == "0002abc123"


def test_real_profiles_tribe_matches_arktribe_files():
    """実サーバーがあれば: 解析したTribeIDが実在の .arktribe ファイル名と一致するはず。無ければskip。"""
    root = r"C:\ArkServers\Ragnarok\ShooterGame\Saved"
    profs = glob.glob(root + r"\**\*.arkprofile", recursive=True)
    if not profs:
        print("  (実プロファイル無し=skip)")
        return
    tribes_from_profile = {arkprofile.parse_profile(f)["tribe_id"]
                           for f in profs} - {None}
    tribe_files = {int(Path(t).stem) for t in
                   glob.glob(root + r"\**\*.arktribe", recursive=True)
                   if Path(t).stem.isdigit()}
    # プロファイルから得たTribeIDは、実在の.arktribeの部分集合であるべき
    assert tribes_from_profile <= tribe_files or not tribe_files, \
        f"profile tribes={tribes_from_profile} tribe_files={tribe_files}"
    # 少なくとも1つは名前が取れる
    assert any(arkprofile.parse_profile(f)["account_name"] for f in profs)


if __name__ == "__main__":
    from tests.run_all import run_module
    run_module(sys.modules[__name__])
