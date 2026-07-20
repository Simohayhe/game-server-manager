"""プレイヤーデータBKの世代管理(players_keep)の回帰テスト。

2026-07-21のバグ: 手動🧬BK / 復元前の安全BK が keep未指定=ワールド用cfg.keep(10)で剪定し、
60世代あったプレイヤーBKを10に激減させた。専用の players_keep(既定60)を全経路が尊重することを固定する。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import backup  # noqa: E402


def _src_entries(tmp: Path):
    """ダミーの .arkprofile を1つ置き、ark_player_backup のソースにする。"""
    saved = tmp / "Saved"
    (saved / "SavedArks").mkdir(parents=True)
    (saved / "SavedArks" / "0002abc.arkprofile").write_bytes(b"dummy")
    return [("TestMap", str(saved), "SavedArks")]


def _make_dummies(target: Path, n: int):
    target.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        fp = target / f"players_old{i:03d}.zip"
        fp.write_text("x")
        t = time.time() - (n - i) * 600      # 10分刻みの過去
        os.utime(fp, (t, t))


def test_manual_and_safety_path_keeps_players_keep():
    """keep未指定(手動🧬BK・復元の安全BKと同経路) → players_keep(60)で保持。旧バグなら10。"""
    tmp = Path(tempfile.mkdtemp())
    entries = _src_entries(tmp)
    cfg = backup.BackupConfig(path=str(tmp / "bk"), keep=10, players_keep=60)
    d = Path(cfg.path) / "ARK" / "_players"
    _make_dummies(d, 65)
    backup.ark_player_backup(entries, None, cfg)     # +1して剪定
    n = len(list(d.glob("players_*.zip")))
    assert n == 60, f"players_keep=60 が効いていない: {n}件"


def test_explicit_keep_overrides():
    """予約ジョブが keep を明示したらそれが優先(60でも5でも)。"""
    tmp = Path(tempfile.mkdtemp())
    entries = _src_entries(tmp)
    cfg = backup.BackupConfig(path=str(tmp / "bk"), keep=10, players_keep=60)
    d = Path(cfg.path) / "ARK" / "_players"
    _make_dummies(d, 65)
    backup.ark_player_backup(entries, None, cfg, keep=5)
    n = len(list(d.glob("players_*.zip")))
    assert n == 5, f"明示keep=5 が優先されていない: {n}件"


def test_players_keep_setting_is_honored():
    """players_keep=10 にしたら10に絞られる(=設定が実際に効いている証明)。"""
    tmp = Path(tempfile.mkdtemp())
    entries = _src_entries(tmp)
    cfg = backup.BackupConfig(path=str(tmp / "bk"), keep=99, players_keep=10)
    d = Path(cfg.path) / "ARK" / "_players"
    _make_dummies(d, 20)
    backup.ark_player_backup(entries, None, cfg)
    n = len(list(d.glob("players_*.zip")))
    assert n == 10, f"players_keep=10 が効いていない: {n}件"


def test_config_default_players_keep_is_60():
    """BackupConfig の既定 players_keep は 60(=10分毎で約10時間ぶん)。"""
    assert backup.BackupConfig().players_keep == 60


if __name__ == "__main__":
    from tests.run_all import run_module
    run_module(sys.modules[__name__])
