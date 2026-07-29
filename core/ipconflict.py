"""IPアドレス競合の検知。

同じIPを設定された複数のVMバックのサーバーが「両方とも起動中」だと、LAN上で
IPアドレスが衝突して両方まともに通信できなくなる。GSMが確実に持っている情報
(各サーバープロファイルの address / vm と、Hyper-VのVM状態)だけで、稼働中の
競合を検知する。

ARKはホスト運用(全マップが同じホストIP・別ポート)なので競合対象外。ここが見るのは
vm と address を持つサーバー(MC/Palworld)のみ。
"""
from __future__ import annotations


def find_conflicts(servers, vm_states: dict) -> list[dict]:
    """稼働中のIP競合を返す。

    servers: config.servers (各 profile は .vm / .address / .name / .display_name / .game)
    vm_states: {vm名: "Running"|"Off"|...}
    戻り値: [{"ip": ip, "servers": [表示名...], "vms": [vm名...]}]
            (同じIPに、異なるVMが2つ以上 Running の場合のみ)
    """
    by_ip: dict[str, list[tuple[str, str]]] = {}
    for p in servers or []:
        vm = getattr(p, "vm", None)
        addr = getattr(p, "address", None)
        if not vm or not addr:
            continue
        if vm_states.get(vm) != "Running":
            continue
        disp = getattr(p, "display_name", None) or getattr(p, "name", vm)
        by_ip.setdefault(addr, []).append((disp, vm))

    conflicts = []
    for ip, entries in by_ip.items():
        vms = sorted({vm for _disp, vm in entries})
        if len(vms) >= 2:                     # 同一IPで異なるVMが2つ以上起動中
            conflicts.append({
                "ip": ip,
                "servers": sorted({disp for disp, _vm in entries}),
                "vms": vms,
            })
    return conflicts
