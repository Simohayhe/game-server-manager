"""RCONのプレイヤー一覧テキストから接続中プレイヤー名を取り出す。

ゲームごとに ListPlayers/ShowPlayers/list の出力形式が違うので、ここで吸収する。
入退室通知(誰がどのサーバーに入ったか)のために、人数だけでなく名前が要る。
"""
from __future__ import annotations

import re

# players()が失敗を示すときの接頭辞/文言(名前ゼロ件と、取得失敗を区別する)
_FAIL_MARKERS = ("RCON接続不可", "RCON未設定", "接続不可")


def player_names(game: str, raw: str | None) -> list[str] | None:
    """接続中のプレイヤー名一覧を返す。取得できなかった時は None(=不明)。

    None と [](=0人) は区別する。None のときは前回の一覧と比較して
    入退室を判定してはいけない(通信失敗を退室と誤検知しないため)。
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text or any(text.startswith(m) for m in _FAIL_MARKERS):
        return None

    if game == "palworld":
        # ShowPlayers = CSV。1行目はヘッダ "name,playeruid,steamid"、以降が各プレイヤー。
        lines = [l for l in text.splitlines() if l.strip()]
        if lines and lines[0].lower().startswith("name,"):
            lines = lines[1:]
        return [l.split(",")[0].strip() for l in lines if l.split(",")[0].strip()]

    if game == "ark":
        # ListPlayers = "0. PlayerName, 0002xxxx" のような行。名前は番号とIDの間。
        names = []
        for line in text.splitlines():
            m = re.match(r"^\s*\d+\.\s*(.+),\s*[0-9A-Fa-f]+\s*$", line)
            if m:
                names.append(m.group(1).strip())
        return names

    # Minecraft(list) = "There are N of a max of M players online: a, b, c"
    m = re.search(r"players online:?\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if m:
        tail = m.group(1).strip()
        return [n.strip() for n in tail.split(",") if n.strip()] if tail else []
    return None
