"""アップデート判定(バージョン比較)の回帰テスト。標準ライブラリのみ、ネット非依存。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.updatecheck import _ver_tuple  # noqa: E402


def test_ver_tuple_basic():
    assert _ver_tuple("v3.1.0") == (3, 1, 0)
    assert _ver_tuple("3.1.0") == (3, 1, 0)


def test_ver_ordering():
    assert _ver_tuple("v3.2.0") > _ver_tuple("v3.1.1")
    assert _ver_tuple("v3.1.10") > _ver_tuple("v3.1.9")     # 数値比較(文字列比較でない)
    assert _ver_tuple("v3.0.0") < _ver_tuple("v3.0.1")


def test_ver_no_number():
    assert _ver_tuple("") == (0,)
    assert _ver_tuple("main") == (0,)


def test_newest_selected_not_latest_created():
    """selfupdate/updatecheck の '最大バージョンを選ぶ' 前提(タグの数値列比較)を固定。"""
    tags = ["v3.0.0", "v3.1.1", "v3.1.0", "v2.0.1", "v3.0.1"]
    newest = max(tags, key=_ver_tuple)
    assert newest == "v3.1.1", newest


if __name__ == "__main__":
    from tests.run_all import run_module
    run_module(sys.modules[__name__])
