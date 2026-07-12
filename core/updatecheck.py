"""GitHub Releases を見て新しいバージョンがあるか調べる(標準ライブラリのみ)。

現行versionと最新リリースの tag_name を数値列で比較する。リリース未作成や
取得失敗でも例外は投げず、error に理由を入れて返す(アプリを止めないため)。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

_API = "https://api.github.com/repos/{repo}/releases/latest"


def _ver_tuple(tag: str) -> tuple:
    """'v1.2.3' → (1, 2, 3)。数字が無ければ (0,)。"""
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(x) for x in nums) if nums else (0,)


def check_latest(repo: str, current: str, timeout: float = 8.0) -> dict:
    """最新リリースと current を比較する。

    戻り値: {current, latest, update_available, url, error}
      error: None=正常 / "no-release"=リリース未作成 / それ以外=失敗理由
    """
    out = {
        "current": current, "latest": None, "update_available": False,
        "url": f"https://github.com/{repo}/releases", "error": None,
    }
    try:
        req = urllib.request.Request(
            _API.format(repo=repo),
            headers={"User-Agent": "game-server-manager",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        tag = data.get("tag_name") or data.get("name")
        out["latest"] = tag
        out["url"] = data.get("html_url") or out["url"]
        if tag and _ver_tuple(tag) > _ver_tuple(current):
            out["update_available"] = True
    except urllib.error.HTTPError as e:
        out["error"] = "no-release" if e.code == 404 else f"http {e.code}"
    except Exception as e:                      # ネット不通など
        out["error"] = str(e)
    return out
