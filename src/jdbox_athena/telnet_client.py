"""Small Telnet client that does not depend on removed ``telnetlib``."""

from __future__ import annotations

import re
import secrets
import socket
import time
from contextlib import suppress
from typing import Tuple

from .errors import TelnetError

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240


class MiniTelnet:
    """Negotiate enough Telnet to log in and run BusyBox shell commands."""

    def __init__(self, host: str, port: int = 23, timeout: float = 10.0) -> None:
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise TelnetError(f"无法连接 Telnet {host}:{port}: {exc}") from exc
        self.sock.settimeout(0.5)
        self.pending = b""
        self.closed = False

    def close(self) -> None:
        """Close the socket; safe to call more than once."""

        if self.closed:
            return
        self.closed = True
        with suppress(OSError):
            self.sock.close()

    def __enter__(self) -> "MiniTelnet":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def send_line(self, text: str = "") -> None:
        """Send one CRLF-terminated line."""

        try:
            self.sock.sendall(text.encode("utf-8") + b"\r\n")
        except OSError as exc:
            raise TelnetError(f"Telnet 写入失败: {exc}") from exc

    def _strip_telnet(self, chunk: bytes) -> bytes:
        data = self.pending + chunk
        self.pending = b""
        output = bytearray()
        index = 0
        while index < len(data):
            if data[index] != IAC:
                output.append(data[index])
                index += 1
                continue
            if index + 1 >= len(data):
                self.pending = data[index:]
                break
            command = data[index + 1]
            if command == IAC:
                output.append(IAC)
                index += 2
                continue
            if command in (DO, DONT, WILL, WONT):
                if index + 2 >= len(data):
                    self.pending = data[index:]
                    break
                option = data[index + 2]
                response = WONT if command in (DO, DONT) else DONT
                with suppress(OSError):
                    self.sock.sendall(bytes((IAC, response, option)))
                index += 3
                continue
            if command == SB:
                end = data.find(bytes((IAC, SE)), index + 2)
                if end == -1:
                    self.pending = data[index:]
                    break
                index = end + 2
                continue
            index += 2
        return bytes(output)

    def _receive(self) -> str:
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return ""
        except OSError as exc:
            raise TelnetError(f"Telnet 读取失败: {exc}") from exc
        if not data:
            raise TelnetError("Telnet 连接已关闭。")
        return self._strip_telnet(data).decode("utf-8", errors="replace")

    def read_for(self, seconds: float) -> str:
        """Collect available text for a bounded interval."""

        deadline = time.monotonic() + seconds
        output = ""
        while time.monotonic() < deadline:
            output += self._receive()
        return output

    def read_until(self, needle: str, timeout: float) -> str:
        """Read until a marker is observed."""

        deadline = time.monotonic() + timeout
        output = ""
        while time.monotonic() < deadline:
            output += self._receive()
            if needle in output:
                return output
        raise TelnetError(f"等待 Telnet 返回超时: {needle}")

    def read_until_pattern(self, pattern: re.Pattern[str], timeout: float) -> str:
        """Read until a compiled regular expression is observed."""

        deadline = time.monotonic() + timeout
        output = ""
        while time.monotonic() < deadline:
            output += self._receive()
            if pattern.search(output):
                return output
        raise TelnetError(f"等待 Telnet 返回超时: {pattern.pattern}")

    def login(self, username: str, password: str, timeout: float = 12.0) -> None:
        """Log in, or accept a Telnet endpoint that already exposes a root shell."""

        self.send_line()
        deadline = time.monotonic() + timeout
        transcript = ""
        username_sent = False
        password_sent = False
        while time.monotonic() < deadline:
            transcript += self._receive()
            lowered = transcript.lower()
            if not username_sent and ("login:" in lowered or "username:" in lowered):
                self.send_line(username)
                username_sent = True
                transcript = ""
                continue
            if not password_sent and "password:" in lowered:
                self.send_line(password)
                password_sent = True
                break
            if re.search(r"(?:^|\n)[^\n]*[#>]\s*$", transcript):
                break
        try:
            output, status = self.exec("printf __ATHENA_BACKUP_READY__", timeout=10.0)
        except TelnetError as exc:
            raise TelnetError(
                "Telnet 端口已开放，但 root 登录失败；请确认用户名及后台/Telnet 密码。"
            ) from exc
        if status != 0 or "__ATHENA_BACKUP_READY__" not in output:
            raise TelnetError(
                "Telnet 端口已开放，但没有获得可用的 root shell；"
                "请确认用户名及后台/Telnet 密码。"
            )

    def exec(self, command: str, timeout: float = 30.0) -> Tuple[str, int]:
        """Run a command and return its captured output and numeric status."""

        suffix = secrets.token_hex(8)
        begin_token = "__ATHENA_BEGIN_" + suffix
        token = "__ATHENA_RC_" + suffix
        wrapped = (
            f"printf '\\n{begin_token}\\n'; {command}; __athena_rc=$?; "
            f"printf '\\n{token}:%s\\n' \"$__athena_rc\""
        )
        self.send_line(wrapped)
        marker = re.compile(re.escape(token) + r":(\d+)")
        data = self.read_until_pattern(marker, timeout=timeout)
        matches = list(marker.finditer(data))
        match = matches[-1] if matches else None
        if not match:
            raise TelnetError("无法解析远程命令退出码。")
        output = data[: match.start()].replace("\r", "")
        # BusyBox Telnet commonly echoes and visually wraps the entire generated
        # command.  The last begin marker is the marker printed by the shell;
        # anything before it is prompt/echo text, regardless of line wrapping.
        begin_matches = list(re.finditer(re.escape(begin_token), output))
        if not begin_matches:
            raise TelnetError("无法定位远程命令输出起点。")
        output = output[begin_matches[-1].end() :]
        return output.strip(), int(match.group(1))

    def run(self, command: str, timeout: float = 30.0, check: bool = True) -> str:
        """Run a command and optionally require status zero."""

        output, status = self.exec(command, timeout=timeout)
        if check and status != 0:
            raise TelnetError(
                f"远程命令失败，exit={status}\nCommand: {command}\nOutput:\n{output}"
            )
        return output
