"""オンラインからのMod検索・バージョン解決・依存解決(Modrinth + CurseForge)。

標準ライブラリのみ。各ソースを共通形に正規化する。
  検索結果 : {source, id, slug, name, downloads, description}
  解決結果 : {source, id, name, version, filename, url, deps:[(source, dep_id), ...]}

Fabric かつ 指定MCバージョン に一致するファイルを選ぶ。必須依存を再帰的に集める。
CurseForge は x-api-key ヘッダが必要(未指定ならCurseForge検索/解決は使えない)。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

MODRINTH = "https://api.modrinth.com/v2"
CURSEFORGE = "https://api.curseforge.com/v1"
_MC_GAME_ID = 432          # CurseForge: Minecraft
_CF_CLASS_MOD = 6          # CurseForge: Mods
_CF_FABRIC = 4             # CurseForge: modLoaderType Fabric
_UA = "game-server-manager (mod manager)"

_name_cache: dict = {}     # (source, id) -> name


class ModSearchError(Exception):
    pass


def _get(url: str, headers: dict | None = None, timeout: int = 25):
    h = {"User-Agent": _UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise ModSearchError("APIキーが無効か権限不足です(CurseForge)") from e
        if e.code == 404:
            raise ModSearchError("見つかりませんでした(404)") from e
        raise ModSearchError(f"HTTP {e.code}") from e


def _cf_headers(api_key: str) -> dict:
    return {"Accept": "application/json", "x-api-key": api_key}


# ---------------------------------------------------------------------------
# Modrinth
# ---------------------------------------------------------------------------
def search_modrinth(query: str, mcver: str, limit: int = 15) -> list[dict]:
    facets = json.dumps([["project_type:mod"], ["categories:fabric"],
                         ["versions:" + mcver]])
    url = (f"{MODRINTH}/search?query={urllib.parse.quote(query)}"
           f"&limit={limit}&index=relevance&facets={urllib.parse.quote(facets)}")
    data = _get(url)
    out = []
    for h in data.get("hits", []):
        _name_cache[("modrinth", h["project_id"])] = h.get("title")
        out.append({"source": "modrinth", "id": h["project_id"],
                    "slug": h.get("slug"), "name": h.get("title"),
                    "downloads": h.get("downloads", 0),
                    "description": h.get("description", "")})
    return out


def _modrinth_name(project_id: str) -> str:
    key = ("modrinth", project_id)
    if key in _name_cache:
        return _name_cache[key]
    try:
        p = _get(f"{MODRINTH}/project/{project_id}")
        _name_cache[key] = p.get("title") or project_id
    except ModSearchError:
        _name_cache[key] = project_id
    return _name_cache[key]


def resolve_modrinth(project_id: str, mcver: str) -> dict:
    loaders = urllib.parse.quote(json.dumps(["fabric"]))
    gv = urllib.parse.quote(json.dumps([mcver]))
    versions = _get(f"{MODRINTH}/project/{project_id}/version"
                    f"?loaders={loaders}&game_versions={gv}")
    if not versions:
        raise ModSearchError(
            f"Modrinth: このMOD({_modrinth_name(project_id)})に "
            f"{mcver}/Fabric 対応版がありません")
    v = versions[0]                                   # 新しい順の先頭
    files = v.get("files", [])
    f = next((x for x in files if x.get("primary")), files[0] if files else None)
    if not f:
        raise ModSearchError("Modrinth: ダウンロードファイルがありません")
    deps = [("modrinth", d["project_id"]) for d in v.get("dependencies", [])
            if d.get("dependency_type") == "required" and d.get("project_id")]
    return {"source": "modrinth", "id": project_id, "name": _modrinth_name(project_id),
            "version": v.get("version_number", "?"),
            "filename": f["filename"], "url": f["url"], "deps": deps}


# ---------------------------------------------------------------------------
# CurseForge
# ---------------------------------------------------------------------------
def search_curseforge(query: str, mcver: str, api_key: str,
                      limit: int = 15) -> list[dict]:
    if not api_key:
        raise ModSearchError("CurseForge APIキーが未設定です")
    q = urllib.parse.urlencode({
        "gameId": _MC_GAME_ID, "classId": _CF_CLASS_MOD, "searchFilter": query,
        "gameVersion": mcver, "modLoaderType": _CF_FABRIC,
        "pageSize": limit, "sortField": 2, "sortOrder": "desc"})
    data = _get(f"{CURSEFORGE}/mods/search?{q}", headers=_cf_headers(api_key))
    out = []
    for m in data.get("data", []):
        _name_cache[("curseforge", m["id"])] = m.get("name")
        out.append({"source": "curseforge", "id": m["id"], "slug": m.get("slug"),
                    "name": m.get("name"), "downloads": m.get("downloadCount", 0),
                    "description": m.get("summary", "")})
    return out


def _cf_name(mod_id, api_key: str) -> str:
    key = ("curseforge", mod_id)
    if key in _name_cache:
        return _name_cache[key]
    try:
        d = _get(f"{CURSEFORGE}/mods/{mod_id}", headers=_cf_headers(api_key))
        _name_cache[key] = d.get("data", {}).get("name") or str(mod_id)
    except ModSearchError:
        _name_cache[key] = str(mod_id)
    return _name_cache[key]


def _forgecdn_url(file_id, filename: str) -> str:
    """downloadUrlがnull(API配布無効)なmod用にCDN URLを組み立てる。"""
    s = str(file_id)
    return (f"https://mediafilez.forgecdn.net/files/"
            f"{s[:4]}/{int(s[4:])}/{urllib.parse.quote(filename)}")


def resolve_curseforge(mod_id, mcver: str, api_key: str) -> dict:
    if not api_key:
        raise ModSearchError("CurseForge APIキーが未設定です")
    q = urllib.parse.urlencode({"gameVersion": mcver, "modLoaderType": _CF_FABRIC,
                                "pageSize": 50})
    data = _get(f"{CURSEFORGE}/mods/{mod_id}/files?{q}", headers=_cf_headers(api_key))
    cand = [f for f in data.get("data", []) if mcver in (f.get("gameVersions") or [])]
    if not cand:
        raise ModSearchError(
            f"CurseForge: このMOD({_cf_name(mod_id, api_key)})に "
            f"{mcver}/Fabric 対応ファイルがありません")
    f = cand[0]                                        # 新しい順の先頭
    url = f.get("downloadUrl") or _forgecdn_url(f["id"], f["fileName"])
    deps = [("curseforge", d["modId"]) for d in (f.get("dependencies") or [])
            if d.get("relationType") == 3]             # 3 = RequiredDependency
    return {"source": "curseforge", "id": mod_id, "name": _cf_name(mod_id, api_key),
            "version": f.get("displayName") or f.get("fileName"),
            "filename": f["fileName"], "url": url, "deps": deps}


# ---------------------------------------------------------------------------
# 共通(検索・解決・依存収集)
# ---------------------------------------------------------------------------
def search(query: str, mcver: str, api_key: str = "",
           sources=("modrinth", "curseforge")) -> list[dict]:
    """両ソースを検索してまとめる(片方が失敗しても続行)。"""
    results, errors = [], []
    if "modrinth" in sources:
        try:
            results += search_modrinth(query, mcver)
        except ModSearchError as e:
            errors.append(f"Modrinth: {e}")
    if "curseforge" in sources and api_key:
        try:
            results += search_curseforge(query, mcver, api_key)
        except ModSearchError as e:
            errors.append(f"CurseForge: {e}")
    results.sort(key=lambda r: r.get("downloads", 0), reverse=True)
    if not results and errors:
        raise ModSearchError(" / ".join(errors))
    return results


def resolve(source: str, mod_id, mcver: str, api_key: str = "") -> dict:
    if source == "modrinth":
        return resolve_modrinth(mod_id, mcver)
    return resolve_curseforge(mod_id, mcver, api_key)


def collect_with_deps(source: str, mod_id, mcver: str, api_key: str = "") -> dict:
    """対象MOD + 必須依存を再帰解決して {(source,id): entry} を返す。

    依存が対象MCに無い等は warnings に積んでスキップ(本体は入れる)。
    戻り値に "__warnings__" キーで警告メッセージのリストを含める。
    """
    seen: dict = {}
    warnings: list[str] = []

    def walk(src, mid):
        key = (src, str(mid))
        if key in seen:
            return
        entry = resolve(src, mid, mcver, api_key)
        seen[key] = entry
        for dsrc, did in entry["deps"]:
            try:
                walk(dsrc, did)
            except ModSearchError as e:
                warnings.append(str(e))

    walk(source, mod_id)
    seen["__warnings__"] = warnings
    return seen


def download(url: str, dest_path, timeout: int = 120) -> int:
    """URLをdest_pathへ保存。zip署名を軽く検証。バイト数を返す。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:2] != b"PK":
        raise ModSearchError(f"jarらしくないファイルです: {url}")
    with open(dest_path, "wb") as fp:
        fp.write(data)
    return len(data)
