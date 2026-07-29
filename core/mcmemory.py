"""Minecraft(Fabric)サーバーのメモリ変更。

  - JVMヒープ(-Xmx/-Xms): systemdのExecStartを書き換えてサービス再起動(VMは触らない)。
    JVMの仕組み上、ヒープ変更は必ずプロセス再起動が必要。
  - VMメモリ: 動的メモリ(Dynamic Memory)なら稼働中に最大をライブ変更できる。
    静的メモリ or 動的化する場合は一度VM停止が必要。

ヒープはVMの割当RAM以下でないと動かないため、動的化する時は VM最小/起動 = ヒープ+1GB に
してJVMが常にヒープぶんのRAMを確保できるようにする。
"""
from __future__ import annotations

import re
import time

from core import modmanager
from core.orchestration import _wait_for_port

OVERHEAD_MB = 1024        # ヒープの外に確保するOS/JVMオーバーヘッド


def _mb_from(token: str) -> int:
    m = re.match(r"(\d+)\s*([GgMmKk]?)", token or "")
    if not m:
        return 0
    n, u = int(m.group(1)), m.group(2).upper()
    if u == "G":
        return n * 1024
    if u == "K":
        return max(1, n // 1024)
    return n                # M または無単位=MB扱い


def _read_heap(profile) -> dict:
    c = modmanager._connect(profile)
    try:
        _, out, _ = c.exec_command(
            f"systemctl cat {profile.service} 2>/dev/null | grep -i ExecStart", timeout=30)
        line = out.read().decode("utf-8", "replace")
    finally:
        c.close()
    xmx = re.search(r"-Xmx(\S+)", line)
    xms = re.search(r"-Xms(\S+)", line)
    return {"xmx_mb": _mb_from(xmx.group(1)) if xmx else 0,
            "xms_mb": _mb_from(xms.group(1)) if xms else 0}


def read(profile, hyperv) -> dict:
    """現在の設定を返す。{heap:{xmx_mb,xms_mb}, vm:{state,dynamic,startup_mb,min_mb,max_mb,assigned_mb}}

    VMが停止中でSSHできない場合は heap を空で返す(VM情報は取れる)。
    """
    try:
        heap = _read_heap(profile)
    except Exception:
        heap = {}                      # VM停止中など。VM側の情報だけでも返す
    vm = hyperv.get_memory(profile.vm) if profile.vm else {}
    return {"heap": heap, "vm": vm, "vm_name": profile.vm}


def _sudo(client, password, script, timeout=180) -> str:
    sftp = client.open_sftp()
    with sftp.open("/tmp/gsm_mcmem.sh", "w") as f:
        f.write(script)
    sftp.close()
    stdin, stdout, _ = client.exec_command(
        "sudo -S -p '' bash /tmp/gsm_mcmem.sh 2>&1", timeout=timeout)
    stdin.write(password + "\n")
    stdin.flush()
    return stdout.read().decode("utf-8", "replace")


def _stop_service(profile):
    c = modmanager._connect(profile)
    try:
        _sudo(c, profile.ssh_password, f"systemctl stop '{profile.service}'")
    finally:
        c.close()


def _set_heap(profile, xmx_mb, xms_mb, progress, restart=True):
    """systemdユニットの -Xmx/-Xms を書き換える。

    restart=True: サービス再起動で即反映。False: 書き換えのみ(次回起動時に反映)。
    停止中のサーバーを勝手に起動しないよう、呼び出し側が元の状態に合わせて選ぶ。
    """
    progress(f"JVMヒープを {xmx_mb}MB に設定"
             + ("してサービス再起動…" if restart else "(次回起動時に反映)…"))
    c = modmanager._connect(profile)
    try:
        tail = f"systemctl restart '{profile.service}'" if restart else "true"
        script = f"""set -e
U='/etc/systemd/system/{profile.service}.service'
sed -i -E 's/-Xmx[0-9]+[GgMmKk]?/-Xmx{int(xmx_mb)}M/' "$U"
if grep -q -- '-Xms' "$U"; then sed -i -E 's/-Xms[0-9]+[GgMmKk]?/-Xms{int(xms_mb)}M/' "$U"; fi
systemctl daemon-reload
{tail}
echo HEAP_OK
"""
        out = _sudo(c, profile.ssh_password, script)
        if "HEAP_OK" not in out:
            raise RuntimeError(f"JVMヒープの変更に失敗:\n{out[-500:]}")
    finally:
        c.close()


def _service_active(profile) -> bool:
    """サービスがactiveか(SSH)。判定できない時はFalse。"""
    try:
        c = modmanager._connect(profile)
        try:
            _, out, _ = c.exec_command(
                f"systemctl is-active {profile.service} 2>/dev/null", timeout=20)
            return out.read().decode("utf-8", "replace").strip() == "active"
        finally:
            c.close()
    except Exception:
        return False


def _wait_ssh(profile, progress, timeout=180):
    progress("VMの起動を待機中…")
    _wait_for_port(profile.address, getattr(profile, "ssh_port", 22), timeout)
    time.sleep(3)


def _start_service(profile):
    c = modmanager._connect(profile)
    try:
        _sudo(c, profile.ssh_password, f"systemctl start '{profile.service}'")
    finally:
        c.close()


def read_vm(profile, hyperv) -> dict:
    """VMメモリ情報のみ(Palworld等のネイティブサーバー用。JVMヒープ無し)。"""
    vm = hyperv.get_memory(profile.vm) if profile.vm else {}
    return {"heap": {}, "vm": vm, "vm_name": profile.vm}


def change_vm_only(profile, hyperv, vm_max_mb, dynamic=True,
                   progress=lambda t: None) -> dict:
    """VMメモリだけを変更する(Palworld等、ヒープ概念が無いサーバー用)。

    動的メモリ+稼働中なら最大値をライブ変更(サービス無停止)。それ以外は
    サービス停止→VM停止→設定→VM起動→サービス起動(ダウンタイムあり)。
    """
    vm = profile.vm
    if not vm:
        raise RuntimeError("このサーバーにはVMが設定されていません")
    vm_max_mb = int(vm_max_mb)
    cur = hyperv.get_memory(vm)
    was_vm_running = cur["state"] == "Running"
    svc_active = _service_active(profile) if was_vm_running else False
    cur_size = cur["max_mb"] if cur["dynamic"] else cur["startup_mb"]
    if dynamic == cur["dynamic"] and vm_max_mb == cur_size:
        progress("変更なし(既に指定どおり)")
        return {"vm": "unchanged"}

    if was_vm_running and cur["dynamic"] and dynamic:
        if vm_max_mb < cur["max_mb"]:
            # Hyper-Vの仕様: 稼働中の動的メモリは最大値を「増やす」ことしかできない
            raise RuntimeError(
                f"稼働中は最大メモリを増やすことしかできません"
                f"({cur['max_mb']/1024:.1f}GB → {vm_max_mb/1024:.1f}GB の縮小は不可)。\n"
                "縮小するにはサーバーとVMを停止してから実行してください。")
        progress("VMメモリをライブ変更中(サービス無停止)…")
        hyperv.set_memory_live(vm, max_mb=vm_max_mb)      # min/startupは維持
        return {"vm": "live"}

    # ---- オフライン適用。終わったら「元の状態」に戻す(停止中なら停止のまま) ----
    if was_vm_running:
        if svc_active:
            progress("サービスを停止中…")
            try:
                _stop_service(profile)
            except Exception:
                pass
        progress(f"VM {vm} を停止中…")
        hyperv.stop_vm(vm, force=False)
    progress("VMメモリを設定中…")
    if dynamic:
        startup = min(cur["startup_mb"] or vm_max_mb, vm_max_mb)
        minmb = min(cur["min_mb"] or startup, vm_max_mb)
        hyperv.set_memory_offline(vm, dynamic=True, startup_mb=startup,
                                  min_mb=minmb, max_mb=vm_max_mb)
    else:
        hyperv.set_memory_offline(vm, dynamic=False, startup_mb=vm_max_mb)
    if not was_vm_running:
        progress("停止中のため設定のみ適用しました(起動はしません。次回起動時に反映)")
        return {"vm": "applied"}
    progress(f"VM {vm} を起動中…")
    hyperv.start_vm(vm)
    _wait_ssh(profile, progress)
    if svc_active:
        progress("サービスを起動中…")
        _start_service(profile)
    else:
        progress("サービスは元々停止していたため起動しません")
    return {"vm": "restarted"}


def change(profile, hyperv, heap_mb, vm_max_mb=None, dynamic=True,
           progress=lambda t: None) -> dict:
    """メモリを変更する。vm_max_mb=None ならJVMヒープのみ変更(VMは触らない)。

    dynamic=True: VMを動的メモリにして最大=vm_max_mb(稼働中なら可能な限りライブ変更)。
    dynamic=False: 静的メモリで RAM=vm_max_mb(要VM停止)。
    """
    heap_mb = int(heap_mb)
    min_mb = heap_mb + OVERHEAD_MB
    vm = profile.vm
    result = {"heap_mb": heap_mb, "vm": "unchanged"}

    cur = hyperv.get_memory(vm) if vm else None
    was_vm_running = bool(cur and cur["state"] == "Running")
    svc_active = _service_active(profile) if (was_vm_running or not vm) else False

    need_vm = live_ok = False
    if cur and vm_max_mb is not None:
        vm_max_mb = int(vm_max_mb)
        cur_size = cur["max_mb"] if cur["dynamic"] else cur["startup_mb"]
        need_vm = (dynamic != cur["dynamic"]) or (vm_max_mb != cur_size)
        live_ok = (was_vm_running and cur["dynamic"] and dynamic
                   and vm_max_mb >= cur["max_mb"])   # 稼働中の縮小は不可→再起動パスへ

    def _heap_on_stopped_vm():
        """VMが停止中: 一時起動してユニットを書き換え、また停止して元に戻す。"""
        progress("VMが停止中のため一時起動してヒープ設定を書き換えます…")
        hyperv.start_vm(vm)
        _wait_ssh(profile, progress)
        try:
            _stop_service(profile)      # enabledで自動起動した分を止める
        except Exception:
            pass
        _set_heap(profile, heap_mb, heap_mb, progress, restart=False)
        progress("VMを停止して元の停止状態に戻します…")
        hyperv.stop_vm(vm, force=False)

    if need_vm:
        if live_ok:
            progress("VMメモリをライブ変更中(再起動なし)…")
            hyperv.set_memory_live(vm, max_mb=vm_max_mb, min_mb=min_mb)
            result["vm"] = "live"
            # サービスが元々停止なら書き換えのみ(勝手に起動しない)
            _set_heap(profile, heap_mb, heap_mb, progress, restart=svc_active)
        else:
            if was_vm_running:
                if svc_active:
                    progress("サービスを停止中…")
                    try:
                        _stop_service(profile)
                    except Exception:
                        pass
                progress(f"VM {vm} を停止中…")
                hyperv.stop_vm(vm, force=False)
            progress("VMメモリを設定中…")
            if dynamic:
                hyperv.set_memory_offline(vm, dynamic=True, startup_mb=min_mb,
                                          min_mb=min_mb, max_mb=vm_max_mb)
            else:
                hyperv.set_memory_offline(vm, dynamic=False, startup_mb=vm_max_mb)
            if was_vm_running:
                progress(f"VM {vm} を起動中…")
                hyperv.start_vm(vm)
                _wait_ssh(profile, progress)
                # ヒープ書き換え+サービスは元の状態に合わせる
                _set_heap(profile, heap_mb, heap_mb, progress, restart=svc_active)
                result["vm"] = "restarted"
            else:
                _heap_on_stopped_vm()
                result["vm"] = "applied"
                progress("停止中のため設定のみ適用しました(次回起動時に反映)")
    else:
        # ヒープのみ変更
        if vm and not was_vm_running:
            _heap_on_stopped_vm()
            result["vm"] = "applied"
            progress("停止中のため設定のみ適用しました(次回起動時に反映)")
        else:
            _set_heap(profile, heap_mb, heap_mb, progress, restart=svc_active)

    return result
