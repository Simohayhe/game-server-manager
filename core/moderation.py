"""プレイヤーのモデレーション(BAN/キック/BAN解除/ホワイトリスト)。

ゲームごとにコマンドが違うので、ここで吸収する。
- Minecraft : RCON(ban/kick/ban-ip/pardon/banlist/whitelist)。要サーバー起動。
- Palworld  : RCON(BanPlayer/KickPlayer)。SteamID指定。要サーバー起動。
- ARK(ASA)  : RCON(BanPlayer/KickPlayer/UnbanPlayer)。**停止中はBanList.txtを直接編集**して
              オフラインの相手もBANできる(次回起動で反映)。EOSID(32桁hex)指定。

BAN一覧: MCはRCON banlist、ARKは BanList.txt を読む(停止中でも可)。
"""
from __future__ import annotations

from pathlib import Path

# アクション名 → RCONコマンド(文字列 or {target}/{reason} を含むテンプレ)
_MC = {
    "kick": "kick {target}{reason}",
    "ban": "ban {target}{reason}",
    "banip": "ban-ip {target}",
    "unban": "pardon {target}",
    "unbanip": "pardon-ip {target}",
    "banlist": "banlist",
    "wl_add": "whitelist add {target}",
    "wl_remove": "whitelist remove {target}",
    "wl_on": "whitelist on",
    "wl_off": "whitelist off",
    "wl_list": "whitelist list",
}
_PAL = {
    "kick": "KickPlayer {target}",
    "ban": "BanPlayer {target}",
}
_ARK_RCON = {
    "kick": "KickPlayer {target}",
    "ban": "BanPlayer {target}",
    "unban": "UnbanPlayer {target}",
    "wl_add": "AllowPlayerToJoinNoCheck {target}",
    "wl_remove": "DisallowPlayerToJoinNoCheck {target}",
}

# GUI表示用: ゲームごとに使えるアクションと、対象に何を入れるか
CAPABILITIES = {
    "minecraft": {"target_hint": "プレイヤー名", "actions": list(_MC)},
    "palworld": {"target_hint": "SteamID", "actions": list(_PAL)},
    "ark": {"target_hint": "EOS ID(32桁) または 名前",
            "actions": ["kick", "ban", "unban", "banlist", "wl_add", "wl_remove"]},
}


class ModerationError(Exception):
    pass


def mc_command(action: str, target: str = "", reason: str = "") -> str:
    tmpl = _MC.get(action)
    if tmpl is None:
        raise ModerationError(f"Minecraftでは未対応の操作です: {action}")
    r = f" {reason}" if reason else ""
    return tmpl.format(target=target, reason=r).strip()


def pal_command(action: str, target: str = "") -> str:
    tmpl = _PAL.get(action)
    if tmpl is None:
        raise ModerationError(f"Palworldでは未対応の操作です: {action}(手動対応)")
    return tmpl.format(target=target).strip()


def ark_rcon_command(action: str, target: str = "") -> str:
    tmpl = _ARK_RCON.get(action)
    if tmpl is None:
        raise ModerationError(f"ARKのRCONでは未対応の操作です: {action}")
    return tmpl.format(target=target).strip()


# ---------------------------------------------------------------------------
# ARK: BanList.txt(停止中でも編集できる) — ASAは Win64 の BanList.txt を読む
# ---------------------------------------------------------------------------
def ark_banlist_path(install_root: str | Path) -> Path:
    return (Path(install_root) / "ShooterGame" / "Binaries" / "Win64" / "BanList.txt")


def ark_banlist_read(install_root: str | Path) -> list[str]:
    p = ark_banlist_path(install_root)
    if not p.exists():
        return []
    ids = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s:
            ids.append(s)
    return ids


def _ark_banlist_write(install_root: str | Path, ids: list[str]) -> None:
    p = ark_banlist_path(install_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 重複を除き順序維持。ARKは1行1ID(改行区切り)。
    seen, out = set(), []
    for i in ids:
        i = i.strip()
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    p.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")


def ark_ban_offline(install_root: str | Path, eos_id: str) -> None:
    eos_id = (eos_id or "").strip()
    if not eos_id:
        raise ModerationError("BANするEOS IDを指定してください")
    ids = ark_banlist_read(install_root)
    if eos_id not in ids:
        ids.append(eos_id)
    _ark_banlist_write(install_root, ids)


def ark_unban_offline(install_root: str | Path, eos_id: str) -> None:
    eos_id = (eos_id or "").strip()
    ids = [i for i in ark_banlist_read(install_root) if i != eos_id]
    _ark_banlist_write(install_root, ids)
