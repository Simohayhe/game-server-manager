"""ゲームサーバーマネージャー起動エントリポイント。"""
import sys
from pathlib import Path
from tkinter import messagebox

from core import compat
from core.config import ConfigError, load_config
from gui import firstrun
from gui.app import App

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
FIRSTRUN_MARKER = BASE_DIR / ".gsm_firstrun_done"


def main() -> None:
    # --- 初回起動 or 非対応環境では、まず動作環境チェック画面を出す ---
    result = compat.check()
    first_run = not FIRSTRUN_MARKER.exists()
    if first_run or not result["suitable"]:
        if firstrun.show(result) != "proceed":
            sys.exit(0)
        try:
            FIRSTRUN_MARKER.write_text("done", encoding="utf-8")
        except OSError:
            pass

    try:
        config = load_config(CONFIG_PATH)
    except ConfigError as exc:
        # --windowed でexe化するとコンソールがないため、エラーはダイアログで出す
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "設定エラー",
            f"{exc}\n\nconfig.yaml を用意してください"
            "(setup で config.yaml.example からコピーされます)。")
        sys.exit(1)

    app = App(config)
    app.mainloop()


if __name__ == "__main__":
    main()
