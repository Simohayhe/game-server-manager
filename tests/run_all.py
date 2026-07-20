"""依存なしのテストランナー。`python tests/run_all.py` で全 test_*.py の test_* を実行。

pytest があれば `pytest tests/` でも動く(各テストは def test_* + assert)。
"""
import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_module(mod) -> tuple[int, int]:
    ok = fail = 0
    for name in sorted(dir(mod)):
        if not name.startswith("test_"):
            continue
        fn = getattr(mod, name)
        if not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS {mod.__name__}.{name}")
            ok += 1
        except Exception:                                  # noqa: BLE001
            print(f"  FAIL {mod.__name__}.{name}")
            traceback.print_exc()
            fail += 1
    return ok, fail


def main() -> int:
    files = sorted(Path(__file__).parent.glob("test_*.py"))
    total_ok = total_fail = 0
    for f in files:
        mod = importlib.import_module(f"tests.{f.stem}")
        print(f"== {f.name} ==")
        ok, fail = run_module(mod)
        total_ok += ok
        total_fail += fail
    print(f"\n結果: {total_ok} passed, {total_fail} failed")
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
