"""コマンド実行のトランスポート層。

管理ソフトから見た「相手」は2種類:
  - Hyper-Vホスト(Windows) … SSH経由でPowerShellを実行
  - ゲームサーバーVM(Linux) … SSH経由でシェルコマンドを実行
開発・検証用にローカルPowerShell実行もサポートする。
"""
from __future__ import annotations

import base64
import socket
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import paramiko

# PowerShellの標準出力をUTF-8に固定するプリアンブル
_PS_UTF8 = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _encode_ps(script: str) -> str:
    """PowerShell -EncodedCommand 用にUTF-16LE + base64エンコードする。

    引用符のエスケープ地獄を避けるため、常にEncodedCommandで渡す。
    """
    return base64.b64encode((_PS_UTF8 + script).encode("utf-16-le")).decode("ascii")


class LocalPowerShell:
    """このPC上でPowerShellを実行する(hyperv.mode: local 用)。"""

    def run_ps(self, script: str, timeout: float = 60) -> CommandResult:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", _encode_ps(script)],
            capture_output=True,
            timeout=timeout,
            # --windowed でexe化したときにコンソールが開かないようにする
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return CommandResult(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    def close(self) -> None:
        pass


class SSHTransport:
    """SSH経由でリモートにコマンドを実行する。接続は遅延確立し、切れたら張り直す。"""

    def __init__(self, host: str, user: str, port: int = 22,
                 key: str | None = None, password: str | None = None,
                 connect_timeout: float = 8):
        self.host = host
        self.user = user
        self.port = port
        self.key = str(Path(key).expanduser()) if key else None
        self.password = password
        self.connect_timeout = connect_timeout
        self._client: paramiko.SSHClient | None = None
        self._lock = threading.Lock()

    def _ensure_connected(self) -> paramiko.SSHClient:
        if self._client is not None:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                return self._client
            self._client.close()
            self._client = None

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.host,
            port=self.port,
            username=self.user,
            key_filename=self.key,
            password=self.password,
            timeout=self.connect_timeout,
            allow_agent=self.key is None and self.password is None,
            look_for_keys=self.password is None,
        )
        self._client = client
        return client

    def run(self, command: str, timeout: float = 60) -> CommandResult:
        with self._lock:
            client = self._ensure_connected()
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            try:
                out = stdout.read().decode("utf-8", "replace")
                err = stderr.read().decode("utf-8", "replace")
                rc = stdout.channel.recv_exit_status()
            except socket.timeout as exc:
                raise TimeoutError(f"コマンドがタイムアウトしました: {command}") from exc
            return CommandResult(rc, out, err)

    def run_ps(self, script: str, timeout: float = 60) -> CommandResult:
        """Windowsホスト上でPowerShellを実行する(デフォルトシェルがcmdでも動く)。"""
        cmd = f"powershell -NoProfile -NonInteractive -EncodedCommand {_encode_ps(script)}"
        return self.run(cmd, timeout=timeout)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
