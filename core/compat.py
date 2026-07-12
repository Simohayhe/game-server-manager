"""このPCで Hyper-V版 のゲームサーバーマネージャーが使えるかを判定する。

Hyper-Vが使えない環境(Home Edition / 未導入 / 非Windows)では、
VMを使わない軽量版へ誘導するための情報を返す。
"""
from __future__ import annotations

import platform

# VMを使わない軽量版の案内先(将来のリポジトリ。今は本体リポを指す)
VMLESS_URL = "https://github.com/Simohayhe/game-server-manager#今後の拡張候補"


def _ps(cmd: str) -> str:
    """ローカルPowerShellを1回実行して stdout を返す(失敗時は空文字)。"""
    try:
        from .transport import LocalPowerShell
        r = LocalPowerShell().run_ps(cmd, timeout=20)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def check() -> dict:
    """動作環境を調べて結果を返す。

    戻り値:
      suitable       : Hyper-V版をそのまま使える
      hyperv_present : Hyper-V機能自体がこのマシンにある(モジュール検出)
      can_get_vm     : Get-VM が実際に成功する(権限含めOK)
      checks         : [(項目名, ok:bool, 補足)] 表示用
      reason         : suitable=False のときの分類 'os' / 'no_hyperv' / 'permission'
    """
    is_windows = platform.system() == "Windows"
    hyperv_present = False
    can_get_vm = False
    vmms_running = False

    if is_windows:
        hyperv_present = _ps(
            "if (Get-Command Get-VM -ErrorAction SilentlyContinue) {'yes'} else {'no'}"
        ) == "yes"
        if hyperv_present:
            can_get_vm = _ps(
                "try { Get-VM -ErrorAction Stop | Out-Null; 'ok' } catch { 'no' }"
            ) == "ok"
            vmms_running = _ps(
                "(Get-Service vmms -ErrorAction SilentlyContinue).Status"
            ) == "Running"

    deps_ok = True
    for mod in ("paramiko", "yaml"):
        try:
            __import__(mod)
        except Exception:
            deps_ok = False

    suitable = bool(is_windows and hyperv_present and can_get_vm)

    if suitable:
        reason = ""
    elif not is_windows:
        reason = "os"
    elif not hyperv_present:
        reason = "no_hyperv"
    else:
        reason = "permission"        # Hyper-Vはあるが Get-VM が通らない

    checks = [
        ("Windows である", is_windows, "Hyper-VはWindows専用"),
        ("Hyper-V 機能が有効", hyperv_present,
         "「Windowsの機能」でHyper-Vを有効化(Pro/Enterprise/Education)"),
        ("Hyper-V サービス稼働", vmms_running, "サービス vmms が Running"),
        ("VM操作の権限あり", can_get_vm,
         "管理者 または Hyper-V Administrators グループ + 再ログオン"),
        ("Python依存ライブラリ", deps_ok, "paramiko / PyYAML(setupで導入)"),
    ]
    return {
        "suitable": suitable, "hyperv_present": hyperv_present,
        "can_get_vm": can_get_vm, "vmms_running": vmms_running,
        "is_windows": is_windows, "deps_ok": deps_ok,
        "reason": reason, "checks": checks, "vmless_url": VMLESS_URL,
    }
