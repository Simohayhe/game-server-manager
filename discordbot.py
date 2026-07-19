"""GSM Discord ボット — ゲームサーバーをDiscordから起動/停止/状態確認する。

GSM本体と同じ config.yaml / core を使う。config.yaml に discord セクションを追加:

    discord:
      token: "BOT_TOKEN"          # Discord Developer Portal で発行
      guild_id: 123456789         # (任意)このサーバーにコマンドを即時同期
      admin_role_id: 123456789    # (任意)このロールだけ操作可。未設定なら管理者権限が必要
      allowed_servers: []         # (任意)操作を許すサーバーのキー/名前。空=全許可
      log_channel_id: 123456789   # (任意)操作ログ(誰が何をしたか)を流すチャンネル

実行:  python discordbot.py   (常時起動しておく)

安全設計:
  - 操作は「許可された人が明示的にコマンドを打った時だけ」実行される(コマンド駆動)。
  - allowed_servers で bot 操作可能なサーバーを絞れる(既定=全許可)。
  - 稼働中サーバーの停止/再起動は、実行前に確認ボタンを必須にする
    (本番のARK/Palworldをうっかり止めてプレイヤーを切断しないため)。
  - 操作は stdout に監査ログを出し、log_channel_id 設定時はそこにも記録する。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path

import discord
import yaml
from discord import app_commands

from core.arkhost import ArkHost
from core.config import load_config
from core.gameserver import GameServer
from core.hyperv import HyperVManager
from core.orchestration import start_server_with_vm
from core.transport import LocalPowerShell, SSHTransport

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


# ---------------------------------------------------------------------------
# 操作対象の共通ラッパー(MC/Palworld=ServerTarget, ARK=ArkTarget)
# ---------------------------------------------------------------------------
class ServerTarget:
    kind = "server"

    def __init__(self, gs: GameServer, hyperv: HyperVManager):
        self.gs = gs
        self.hyperv = hyperv
        self.key = gs.profile.name
        self.label = gs.profile.display_name

    def is_running(self) -> bool:
        return self.gs.status() == "active"

    def start(self, log):
        start_server_with_vm(self.hyperv, self.gs, progress=log)

    def stop(self, log):
        log("停止中…")
        self.gs.stop()

    def restart(self, log):
        log("停止中…")
        self.gs.stop()
        time.sleep(2)
        start_server_with_vm(self.hyperv, self.gs, progress=log)

    def players(self) -> str:
        raw = self.gs.players()
        pp = self.gs.parse_players(raw)
        if pp:
            n, mx, names = pp
            return f"{n}/{mx if mx is not None else '?'}" + (f"  {names}" if names else "")
        return (raw or "").strip()[:150]

    def address(self) -> str:
        p = self.gs.profile
        host = getattr(p, "fqdn", None) or p.address
        port = getattr(p, "external_port", None) or p.game_port
        return f"{host}:{port}"


class ArkTarget:
    kind = "ark"

    def __init__(self, ah: ArkHost):
        self.ah = ah
        self.key = _slug(ah.cfg.display_name)   # 例: ark-the-island
        self.label = ah.cfg.display_name

    def is_running(self) -> bool:
        return self.ah.is_running()

    def start(self, log):
        self.ah.start(progress=log)

    def stop(self, log):
        self.ah.stop(progress=log)

    def restart(self, log):
        self.ah.restart(progress=log)

    def players(self) -> str:
        try:
            return f"{self.ah.num_players()} 人"
        except Exception:
            return (self.ah.players() or "").strip()[:150]

    def address(self) -> str:
        port = getattr(self.ah.cfg, "game_port", None)
        return f"(ARK) Port {port}" if port else ""


# ---------------------------------------------------------------------------
# 設定読み込み + ターゲット構築
# ---------------------------------------------------------------------------
cfg = load_config(CONFIG_PATH)
_raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
_dconf = _raw.get("discord") or {}
TOKEN = _dconf.get("token")
GUILD_ID = _dconf.get("guild_id")
ADMIN_ROLE_ID = _dconf.get("admin_role_id")
LOG_CHANNEL_ID = _dconf.get("log_channel_id")
# 操作を許すサーバー(キー or 表示名)。空/未設定なら全許可。
_ALLOWED = {str(x).strip().lower() for x in (_dconf.get("allowed_servers") or [])}

if cfg.hyperv.mode == "local":
    _runner = LocalPowerShell()
else:
    _runner = SSHTransport(host=cfg.hyperv.host, user=cfg.hyperv.user,
                           port=cfg.hyperv.port, key=cfg.hyperv.key,
                           password=cfg.hyperv.password)
_hyperv = HyperVManager(_runner)


def _is_allowed(t) -> bool:
    if not _ALLOWED:
        return True
    return t.key.lower() in _ALLOWED or t.label.lower() in _ALLOWED


TARGETS: dict = {}
for _p in cfg.servers:
    _t = ServerTarget(GameServer(_p), _hyperv)
    if _is_allowed(_t):
        TARGETS[_t.key] = _t
for _c in cfg.ark_hosts:
    _t = ArkTarget(ArkHost(_c, _runner))
    if _is_allowed(_t):
        TARGETS[_t.key] = _t


def authorized(interaction: discord.Interaction) -> bool:
    if ADMIN_ROLE_ID:
        roles = getattr(interaction.user, "roles", [])
        return any(getattr(r, "id", None) == int(ADMIN_ROLE_ID) for r in roles)
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


# ---------------------------------------------------------------------------
# Discord クライアント & コマンド
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def _audit(interaction: discord.Interaction, label: str, verb: str,
                 ok: bool, err: str = "") -> None:
    """操作を stdout と(設定時)ログチャンネルに記録する。"""
    who = getattr(interaction.user, "display_name", str(interaction.user))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mark = "OK" if ok else "NG"
    line = f"[{ts}] [{mark}] {who} → {label} を{verb}" + (f" (失敗: {err})" if err else "")
    print(line, flush=True)
    if LOG_CHANNEL_ID:
        try:
            ch = client.get_channel(int(LOG_CHANNEL_ID)) \
                or await client.fetch_channel(int(LOG_CHANNEL_ID))
            emoji = "✅" if ok else "❌"
            await ch.send(f"{emoji} `{who}` が **{label}** を{verb}"
                          + (f"（失敗: {err}）" if err else ""))
        except Exception as exc:                               # noqa: BLE001
            print("ログチャンネル送信に失敗:", exc, flush=True)


class _ConfirmView(discord.ui.View):
    """稼働中サーバーの停止/再起動に対する確認ボタン。押した本人のみ有効。"""

    def __init__(self, author_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.value: bool | None = None
        self._author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "あなた宛の確認ではありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="実行する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.defer()


async def _target_ac(interaction: discord.Interaction, current: str):
    cur = current.lower()
    out = [app_commands.Choice(name=t.label, value=t.key)
           for t in TARGETS.values() if cur in t.label.lower()]
    return out[:25]


async def _run_op(interaction: discord.Interaction, key: str, verb: str, method: str):
    if not authorized(interaction):
        await interaction.response.send_message("⛔ 権限がありません。", ephemeral=True)
        return
    t = TARGETS.get(key)
    if t is None:
        await interaction.response.send_message("サーバーが見つかりません。", ephemeral=True)
        return

    running = await asyncio.to_thread(t.is_running)

    # 停止なのに既に止まっている / 再起動なのに止まっている場合の分岐
    if method == "stop" and not running:
        await interaction.response.send_message(
            f"⚪ **{t.label}** は既に停止中です。", ephemeral=True)
        return
    if method == "restart" and not running:
        method, verb = "start", "起動"   # 止まっているものの再起動＝起動

    # 稼働中の停止/再起動は確認ボタンを挟む(プレイヤー切断防止)
    if method in ("stop", "restart") and running:
        view = _ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ **{t.label}** は稼働中です。{verb}するとプレイヤーが切断されます。実行しますか?",
            view=view, ephemeral=True)
        await view.wait()
        for c in view.children:
            c.disabled = True
        try:
            await interaction.edit_original_response(view=view)
        except Exception:                                      # noqa: BLE001
            pass
        if not view.value:
            reason = "タイムアウト" if view.value is None else "キャンセル"
            await interaction.followup.send(
                f"↩️ **{t.label}** の{verb}を中止しました（{reason}）。", ephemeral=True)
            return
        await interaction.followup.send(f"⏳ **{t.label}** を{verb}しています…")
    else:
        await interaction.response.defer(thinking=True)

    logs: list[str] = []
    try:
        await asyncio.to_thread(getattr(t, method), logs.append)
    except Exception as exc:                                   # noqa: BLE001
        await interaction.followup.send(f"❌ **{t.label}** の{verb}に失敗: {exc}")
        await _audit(interaction, t.label, verb, ok=False, err=str(exc))
        return

    extra = ""
    if verb in ("起動", "再起動"):
        addr = t.address()
        if addr:
            extra = f"\n接続先: `{addr}`"
    who = getattr(interaction.user, "display_name", "")
    await interaction.followup.send(f"✅ **{t.label}** を{verb}しました（{who}）。{extra}")
    await _audit(interaction, t.label, verb, ok=True)


gs = app_commands.Group(name="gs", description="ゲームサーバー操作")


@gs.command(name="list", description="サーバー一覧と稼働状況")
async def gs_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    lines = []
    for t in TARGETS.values():
        running = await asyncio.to_thread(t.is_running)
        lines.append(f"{'🟢' if running else '⚪'} **{t.label}** — "
                     f"{'稼働中' if running else '停止中'}")
    await interaction.followup.send("\n".join(lines) or "操作可能なサーバーがありません。")


@gs.command(name="status", description="サーバーの状態と人数")
@app_commands.autocomplete(server=_target_ac)
async def gs_status(interaction: discord.Interaction, server: str):
    t = TARGETS.get(server)
    if t is None:
        await interaction.response.send_message("見つかりません。", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    running = await asyncio.to_thread(t.is_running)
    msg = f"{'🟢 稼働中' if running else '⚪ 停止中'} — **{t.label}**"
    if running:
        try:
            msg += f"\n👥 {await asyncio.to_thread(t.players)}"
        except Exception:
            pass
        if t.address():
            msg += f"\n接続先: `{t.address()}`"
    await interaction.followup.send(msg)


@gs.command(name="start", description="サーバーを起動")
@app_commands.autocomplete(server=_target_ac)
async def gs_start(interaction: discord.Interaction, server: str):
    await _run_op(interaction, server, "起動", "start")


@gs.command(name="stop", description="サーバーを停止")
@app_commands.autocomplete(server=_target_ac)
async def gs_stop(interaction: discord.Interaction, server: str):
    await _run_op(interaction, server, "停止", "stop")


@gs.command(name="restart", description="サーバーを再起動")
@app_commands.autocomplete(server=_target_ac)
async def gs_restart(interaction: discord.Interaction, server: str):
    await _run_op(interaction, server, "再起動", "restart")


tree.add_command(gs)


@client.event
async def on_ready():
    try:
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=g)
            await tree.sync(guild=g)
            print(f"コマンド同期: guild {GUILD_ID}")
        elif client.guilds:
            # 参加中の各サーバーへ即時同期(グローバル同期は反映が遅いため)
            for g in client.guilds:
                tree.copy_global_to(guild=g)
                await tree.sync(guild=g)
            print(f"コマンド同期: 参加中の {len(client.guilds)} サーバー "
                  f"({', '.join(g.name for g in client.guilds)})")
        else:
            await tree.sync()
            print("コマンド同期: グローバル(反映に時間がかかる場合あり)")
    except Exception as exc:                                   # noqa: BLE001
        print("コマンド同期に失敗:", exc)
    allowed = "全許可" if not _ALLOWED else f"{len(TARGETS)}件に限定"
    print(f"ログイン成功: {client.user}  対象サーバー {len(TARGETS)}件({allowed})")


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "config.yaml の discord.token が未設定です。"
            "Discord Developer Portal でBotを作ってトークンを設定してください。")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
