"""ARK(ASA)の設定ファイル編集モデル。

ARKは GameUserSettings.ini と Game.ini の2ファイルにセクション別で設定を持つ。
このマシン(ホスト)上のローカルファイルなのでSSHは不要。
既存の内容(コメント・並び順・配列キー[..]・重複するConfigOverride行・未知キー)は
一切壊さず、指定した key=value のみを差し替える(無ければセクションに追記)。

※ config_dir は全マップで共有(Server1のSaved/Config/WindowsServer)。
   ここを編集すると全マップに効く。反映にはサーバー再起動が必要。
"""
from __future__ import annotations

from pathlib import Path

GAMEMODE = "/Script/ShooterGame.ShooterGameMode"
SERVER_SETTINGS = "ServerSettings"
GAME_SESSION = "/Script/Engine.GameSession"


class ArkIni:
    """セクション対応の .ini。行リストで保持し、key単位で最小差し替えする。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        raw = self.path.read_text(encoding="utf-8", errors="replace") \
            if self.path.exists() else ""
        self._newline = "\r\n" if "\r\n" in raw else "\n"
        self.lines = [ln.rstrip("\r") for ln in raw.split("\n")]
        # 末尾の空行はtext()側で付け直すのでここでは1つ落とす
        if self.lines and self.lines[-1] == "":
            self.lines.pop()

    # ---- 内部 ----
    @staticmethod
    def _is_header(s: str) -> str | None:
        s = s.strip()
        if s.startswith("[") and s.endswith("]"):
            return s[1:-1]
        return None

    def _find(self, section: str, key: str) -> int:
        """section内の key=value 行のインデックス。無ければ-1。配列キー[..]は対象外。"""
        cur = None
        keyl = key.lower()
        for i, line in enumerate(self.lines):
            sec = self._is_header(line)
            if sec is not None:
                cur = sec
                continue
            s = line.strip()
            if cur == section and "=" in s and not s.startswith(";"):
                k = s.split("=", 1)[0].strip()
                if k.lower() == keyl:
                    return i
        return -1

    def _header_index(self, section: str) -> int:
        for i, line in enumerate(self.lines):
            if self._is_header(line) == section:
                return i
        return -1

    # ---- 公開API ----
    def get(self, section: str, key: str, default: str | None = None) -> str | None:
        i = self._find(section, key)
        if i < 0:
            return default
        return self.lines[i].split("=", 1)[1].strip()

    def set(self, section: str, key: str, value: str) -> None:
        i = self._find(section, key)
        if i >= 0:
            self.lines[i] = f"{key}={value}"
            return
        hdr = self._header_index(section)
        if hdr < 0:                      # セクションごと新設(末尾)
            if self.lines and self.lines[-1].strip() != "":
                self.lines.append("")
            self.lines.append(f"[{section}]")
            self.lines.append(f"{key}={value}")
        else:                            # セクション見出し直後に挿入
            self.lines.insert(hdr + 1, f"{key}={value}")

    def remove(self, section: str, key: str) -> bool:
        """section内の key=value 行を削除する。削除したらTrue。"""
        i = self._find(section, key)
        if i < 0:
            return False
        del self.lines[i]
        return True

    def text(self) -> str:
        return self._newline.join(self.lines) + self._newline

    def save(self) -> None:
        self.path.write_text(self.text(), encoding="utf-8")


def gus_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / "GameUserSettings.ini"


def game_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / "Game.ini"


def load(config_dir: str | Path) -> tuple[ArkIni, ArkIni]:
    """(GameUserSettings.ini, Game.ini) を読み込んで返す。"""
    return ArkIni(gus_path(config_dir)), ArkIni(game_path(config_dir))
