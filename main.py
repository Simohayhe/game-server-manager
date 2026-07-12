"""ゲームサーバーマネージャー起動エントリポイント。"""
import sys
from pathlib import Path
from tkinter import messagebox

from core.config import ConfigError, load_config
from gui.app import App

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def main() -> None:
    try:
        config = load_config(CONFIG_PATH)
    except ConfigError as exc:
        # --windowed でexe化するとコンソールがないため、エラーはダイアログで出す
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("設定エラー", str(exc))
        sys.exit(1)

    app = App(config)
    app.mainloop()


if __name__ == "__main__":
    main()
