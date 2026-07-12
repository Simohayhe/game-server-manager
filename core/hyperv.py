"""Hyper-Vホスト上のVM操作。

PowerShellのHyper-Vコマンドレット(Get-VM / Start-VM / Stop-VM)を
トランスポート(ローカル or SSH)経由で実行する。
実行ユーザーはホスト側で管理者または Hyper-V Administrators グループに
所属している必要がある。
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class VMInfo:
    name: str
    state: str        # Running / Off / Paused / Saved など
    cpu_usage: int    # %
    memory_mb: int
    uptime_sec: int
    mac: str | None = None      # NICのMAC(区切りなし大文字)。ARP逆引き用
    ip_hint: str | None = None  # 統合サービスがゲストから報告したIPv4(無い場合が多い)

    @property
    def uptime_text(self) -> str:
        if self.uptime_sec <= 0:
            return "-"
        h, rem = divmod(self.uptime_sec, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}時間{m:02d}分" if h else f"{m}分"


class HyperVError(Exception):
    pass


def _quote(name: str) -> str:
    """PowerShellシングルクォート文字列としてVM名をエスケープする。"""
    return "'" + name.replace("'", "''") + "'"


class HyperVManager:
    def __init__(self, runner):
        """runner: run_ps(script, timeout) -> CommandResult を持つオブジェクト"""
        self._runner = runner

    def list_vms(self) -> list[VMInfo]:
        script = (
            "Get-VM | ForEach-Object {"
            " $na = Get-VMNetworkAdapter -VMName $_.Name | Select-Object -First 1;"
            " [PSCustomObject]@{"
            " Name=$_.Name; State=$_.State.ToString();"
            " CPUUsage=[int]$_.CPUUsage;"
            " MemoryMB=[int]($_.MemoryAssigned/1MB);"
            " UptimeSec=[int]$_.Uptime.TotalSeconds;"
            " Mac=$na.MacAddress;"
            " IPs=((@($na.IPAddresses) -match '^\\d+\\.') -join ',')"
            " } } | ConvertTo-Json -Compress"
        )
        result = self._runner.run_ps(script, timeout=30)
        if not result.ok:
            raise HyperVError(f"VM一覧の取得に失敗しました: {result.stderr.strip()}")
        text = result.stdout.strip()
        if not text:
            return []
        data = json.loads(text)
        if isinstance(data, dict):  # VMが1台のときConvertTo-Jsonは配列にしない
            data = [data]
        return [
            VMInfo(
                name=item["Name"],
                state=item["State"],
                cpu_usage=item.get("CPUUsage") or 0,
                memory_mb=item.get("MemoryMB") or 0,
                uptime_sec=item.get("UptimeSec") or 0,
                mac=item.get("Mac") or None,
                ip_hint=(item.get("IPs") or "").split(",")[0] or None,
            )
            for item in data
        ]

    def set_vm_resources(self, name: str, memory_mb: int | None = None,
                         cpu_count: int | None = None) -> None:
        """メモリ/CPU数を変更する(VMは停止中であること)。"""
        q = _quote(name)
        cmds = [f"$vm = Get-VM -Name {q}",
                "if ($vm.State -ne 'Off') { throw 'VMを停止してから変更してください' }"]
        if memory_mb is not None:
            cmds.append(
                f"Set-VMMemory -VMName {q} -DynamicMemoryEnabled $false"
                f" -StartupBytes {int(memory_mb)}MB")
        if cpu_count is not None:
            cmds.append(f"Set-VMProcessor -VMName {q} -Count {int(cpu_count)}")
        cmds.append("'OK'")
        result = self._runner.run_ps("; ".join(cmds), timeout=60)
        if not result.ok or "OK" not in result.stdout:
            raise HyperVError(f"VM設定の変更に失敗しました: {result.stderr.strip()}")

    def clone_vm(self, template: str, new_name: str,
                 memory_mb: int, cpu_count: int) -> None:
        """テンプレートVMのディスクを複製して新VMを作成し、起動する。

        テンプレートは停止中であること(チェックポイントなし=単一.vhdx)。
        新VMは「テンプレートの親フォルダ\\新VM名」に全ファイルを格納する
        (例: C:\\VM\\ubuntu_template → C:\\VM\\<新VM名>)。
        新VMのMACは動的割り当て(一意)になるため、テンプレート側のnetplanは
        MAC非依存(インターフェース名ベース)であること。
        """
        qt, qn = _quote(template), _quote(new_name)
        script = (
            f"$ErrorActionPreference = 'Stop'; "
            f"$t = Get-VM -Name {qt}; "
            f"if ($t.State -ne 'Off') {{ throw 'テンプレートVMを停止してください' }}; "
            f"if (Get-VM -Name {qn} -ErrorAction SilentlyContinue) {{ throw '同名のVMが既に存在します' }}; "
            f"$disk = (Get-VMHardDiskDrive -VMName {qt} | Select-Object -First 1).Path; "
            f"if ($disk -notmatch '\\.vhdx?$') {{ throw ('テンプレートにチェックポイントが残っています(' + $disk + ')。チェックポイントを削除してから実行してください') }}; "
            f"$base = Split-Path $t.Path -Parent; "
            f"$vmdir = Join-Path $base {qn}; "
            f"$diskdir = Join-Path $vmdir 'Virtual Hard Disks'; "
            f"New-Item -ItemType Directory -Force $diskdir | Out-Null; "
            f"$new = Join-Path $diskdir ({qn} + [IO.Path]::GetExtension($disk)); "
            f"if (Test-Path $new) {{ throw ('ディスクが既に存在します: ' + $new) }}; "
            f"Copy-Item $disk $new; "
            f"$sw = (Get-VMNetworkAdapter -VMName {qt} | Select-Object -First 1).SwitchName; "
            # New-VMは -Path配下に「VM名\\Virtual Machines」を作るため、-Pathには親を渡す
            f"New-VM -Name {qn} -Path $base -MemoryStartupBytes {int(memory_mb)}MB"
            f" -Generation $t.Generation -VHDPath $new -SwitchName $sw | Out-Null; "
            f"Set-VM -Name {qn} -AutomaticCheckpointsEnabled $false; "
            f"Set-VMMemory -VMName {qn} -DynamicMemoryEnabled $false; "
            f"Set-VMProcessor -VMName {qn} -Count {int(cpu_count)}; "
            f"if ($t.Generation -eq 2) {{ "
            f"$fw = Get-VMFirmware -VMName {qt}; "
            f"if ($fw.SecureBootTemplate) {{ Set-VMFirmware -VMName {qn}"
            f" -EnableSecureBoot $fw.SecureBoot -SecureBootTemplate $fw.SecureBootTemplate }}"
            f" else {{ Set-VMFirmware -VMName {qn} -EnableSecureBoot $fw.SecureBoot }} }}; "
            f"Start-VM -Name {qn}; 'CLONE_OK'"
        )
        result = self._runner.run_ps(script, timeout=900)
        if not result.ok or "CLONE_OK" not in result.stdout:
            raise HyperVError(f"VMクローンに失敗しました: {result.stderr.strip()}")

    def duplicate_vm(self, source: str, new_name: str,
                     memory_mb: int, cpu_count: int, start: bool = True) -> None:
        """任意のVMを複製する(ゲーム構築なしの素コピー)。

        clone_vmと違い、差分(Differencing)ディスクは Convert-VHD で平坦化して
        独立したフルディスクにコピーする(単一vhdxはそのままコピー)。
        複数ディスクにも対応。複製元は停止中であること。
        新VMは MAC・IDが新規になる。startがTrueなら起動する(個体化のため)。
        """
        qs, qn = _quote(source), _quote(new_name)
        script = (
            f"$ErrorActionPreference = 'Stop'; "
            f"$s = Get-VM -Name {qs}; "
            f"if ($s.State -ne 'Off') {{ throw '複製元VMを停止してください' }}; "
            f"if (Get-VM -Name {qn} -ErrorAction SilentlyContinue) {{ throw '同名のVMが既に存在します' }}; "
            f"$disks = @(Get-VMHardDiskDrive -VMName {qs}); "
            f"if ($disks.Count -eq 0) {{ throw '複製元にディスクがありません' }}; "
            f"foreach ($d in $disks) {{ if ($d.Path -notmatch '\\.a?vhdx?$') {{ throw ('未対応のディスク形式です: ' + $d.Path) }} }}; "
            f"$base = Split-Path $s.Path -Parent; "
            f"$vmdir = Join-Path $base {qn}; "
            f"$diskdir = Join-Path $vmdir 'Virtual Hard Disks'; "
            f"New-Item -ItemType Directory -Force $diskdir | Out-Null; "
            f"$newdisks = @(); $i = 0; "
            f"foreach ($d in $disks) {{ "
            f"  $ext = [IO.Path]::GetExtension($d.Path) -replace '^\\.a', '.'; "
            f"  if ($i -eq 0) {{ $nm = {qn} + $ext }} else {{ $nm = {qn} + '_' + ($i+1) + $ext }}; "
            f"  $dest = Join-Path $diskdir $nm; "
            f"  if (Test-Path $dest) {{ throw ('ディスクが既に存在: ' + $dest) }}; "
            f"  $parent = (Get-VHD -Path $d.Path).ParentPath; "
            f"  if ($parent) {{ Convert-VHD -Path $d.Path -DestinationPath $dest -VHDType Dynamic }} "
            f"  else {{ Copy-Item $d.Path $dest }}; "
            f"  $newdisks += $dest; $i++ "
            f"}}; "
            f"$sw = (Get-VMNetworkAdapter -VMName {qs} | Select-Object -First 1).SwitchName; "
            f"New-VM -Name {qn} -Path $base -MemoryStartupBytes {int(memory_mb)}MB"
            f" -Generation $s.Generation -VHDPath $newdisks[0] -SwitchName $sw | Out-Null; "
            f"for ($j=1; $j -lt $newdisks.Count; $j++) {{ Add-VMHardDiskDrive -VMName {qn} -Path $newdisks[$j] }}; "
            f"Set-VM -Name {qn} -AutomaticCheckpointsEnabled $false; "
            f"Set-VMMemory -VMName {qn} -DynamicMemoryEnabled $false; "
            f"Set-VMProcessor -VMName {qn} -Count {int(cpu_count)}; "
            f"if ($s.Generation -eq 2) {{ "
            f"$fw = Get-VMFirmware -VMName {qs}; "
            f"if ($fw.SecureBootTemplate) {{ Set-VMFirmware -VMName {qn}"
            f" -EnableSecureBoot $fw.SecureBoot -SecureBootTemplate $fw.SecureBootTemplate }}"
            f" else {{ Set-VMFirmware -VMName {qn} -EnableSecureBoot $fw.SecureBoot }} }}; "
            + (f"Start-VM -Name {qn}; " if start else "")
            + "'DUP_OK'"
        )
        result = self._runner.run_ps(script, timeout=1800)
        if not result.ok or "DUP_OK" not in result.stdout:
            raise HyperVError(f"VM複製に失敗しました: {result.stderr.strip()}")

    def individualize_windows(self, vm_name: str, guest_user: str, guest_pass: str,
                              hostname: str, new_ip: str, gateway: str, dns: str,
                              prefix_len: int = 24, progress=lambda t: None) -> None:
        """WindowsゲストをPowerShell Direct で個体化する(ホスト名・IP変更→再起動)。

        SIDは変更しない(ワークグループ用途では実害なし。ドメイン参加やsysprepは別途)。
        SSH不要(ホストがHyper-V管理者=PowerShell Directが使える前提)。
        """
        qn = _quote(vm_name)
        cred = (
            f"$pw = ConvertTo-SecureString '{guest_pass}' -AsPlainText -Force; "
            f"$cred = New-Object System.Management.Automation.PSCredential('{guest_user}',$pw); "
        )
        progress(f"{vm_name}: ゲスト起動待ち(PowerShell Direct)…")
        wait_boot = (
            "$ErrorActionPreference='SilentlyContinue'; " + cred +
            "$ok=$false; for ($i=0; $i -lt 40; $i++) { Start-Sleep 6; "
            f"try {{ $h = Invoke-Command -VMName {qn} -Credential $cred "
            "-ScriptBlock { $env:COMPUTERNAME } 2>$null; if ($h) { $ok=$true; break } } catch {} }; "
            "if ($ok) { 'BOOT_OK' } else { 'BOOT_TIMEOUT' }"
        )
        rb = self._runner.run_ps(wait_boot, timeout=280)
        if "BOOT_OK" not in rb.stdout:
            raise HyperVError(
                f"{vm_name} のゲスト起動を確認できませんでした(PowerShell Direct無応答)")

        progress(f"{vm_name}: IPアドレスを変更中…")
        ipscript = (
            "$ErrorActionPreference='Stop'; " + cred +
            f"Invoke-Command -VMName {qn} -Credential $cred -ArgumentList "
            f"'{new_ip}','{gateway}','{dns}',{int(prefix_len)} -ScriptBlock {{ "
            "param($ip,$gw,$dns,$plen); "
            "$a = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | Select-Object -First 1; "
            "Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue "
            "| Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue; "
            "Remove-NetRoute -InterfaceIndex $a.ifIndex -Confirm:$false -ErrorAction SilentlyContinue; "
            "New-NetIPAddress -InterfaceIndex $a.ifIndex -IPAddress $ip -PrefixLength $plen -DefaultGateway $gw | Out-Null; "
            "Set-DnsClientServerAddress -InterfaceIndex $a.ifIndex -ServerAddresses $dns; "
            "}; 'IP_OK'"
        )
        r1 = self._runner.run_ps(ipscript, timeout=90)
        if not r1.ok or "IP_OK" not in r1.stdout:
            raise HyperVError(f"Windows個体化(IP変更)に失敗: {r1.stderr.strip()}")

        # ホスト名変更は IP変更と分けて、Rename-Computer -Restart で原子的に(リネーム+再起動)。
        progress(f"{vm_name}: ホスト名を変更して再起動中…")
        rn = (
            cred + "try { " +
            f"Invoke-Command -VMName {qn} -Credential $cred -ArgumentList '{hostname}' "
            "-ScriptBlock { param($hn); Rename-Computer -NewName $hn -Force -Restart } } catch {}; 'RN_SENT'"
        )
        self._runner.run_ps(rn, timeout=90)

        # 新ホスト名で応答するまで待つ(最大~4.5分。Windows再起動は時間がかかる)
        target = hostname.upper()
        wait = (
            "$ErrorActionPreference='SilentlyContinue'; " + cred +
            "$ok=$false; for ($i=0; $i -lt 45; $i++) { Start-Sleep 6; "
            f"try {{ $h = Invoke-Command -VMName {qn} -Credential $cred "
            "-ScriptBlock { $env:COMPUTERNAME } 2>$null; "
            f"if ($h -eq '{target}') {{ $ok=$true; break }} }} catch {{}} }}; "
            "if ($ok) { 'IND_OK' } else { 'IND_TIMEOUT' }"
        )
        progress(f"{vm_name}: 起動待ち(新ホスト名 {hostname})…")
        r2 = self._runner.run_ps(wait, timeout=300)
        if "IND_OK" not in r2.stdout:
            raise HyperVError(
                f"個体化後の起動確認がタイムアウト(手動で {new_ip} / ホスト名を確認してください)")

    def start_vm(self, name: str) -> None:
        result = self._runner.run_ps(f"Start-VM -Name {_quote(name)}", timeout=60)
        if not result.ok:
            raise HyperVError(f"VM起動に失敗しました: {result.stderr.strip()}")

    def stop_vm(self, name: str, force: bool = False) -> None:
        # forceなし = ゲストOSに正規のシャットダウンを要求する
        flag = " -TurnOff" if force else ""
        result = self._runner.run_ps(f"Stop-VM -Name {_quote(name)}{flag}", timeout=120)
        if not result.ok:
            raise HyperVError(f"VM停止に失敗しました: {result.stderr.strip()}")
