from __future__ import annotations

import re
import unittest

from jdbox_athena.telnet_client import DONT, IAC, WILL, MiniTelnet


class DummySocket:
    def __init__(self) -> None:
        self.sent = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)


class EchoingCommandClient(MiniTelnet):
    """Return a transcript shaped like the AX6600 BusyBox Telnet server."""

    def __init__(self, command_output: str) -> None:
        self.command_output = command_output
        self.wrapped = ""

    def send_line(self, text: str = "") -> None:
        self.wrapped = text

    def read_until_pattern(self, _pattern: re.Pattern[str], timeout: float) -> str:
        del timeout
        begin = re.search(r"__ATHENA_BEGIN_[0-9a-f]+", self.wrapped)
        result = re.search(r"__ATHENA_RC_[0-9a-f]+", self.wrapped)
        if begin is None or result is None:
            raise AssertionError("generated wrapper did not contain output markers")
        # The first two lines simulate an 80-column terminal wrapping the echoed
        # command.  The second begin token is the actual printf output.
        return (
            self.wrapped[:72]
            + "\r\n"
            + self.wrapped[72:]
            + "\r\n"
            + begin.group(0)
            + "\r\n"
            + self.command_output
            + "\r\n"
            + result.group(0)
            + ":0\r\n"
        )


class TelnetProtocolTests(unittest.TestCase):
    def test_negotiation_is_removed_and_rejected(self) -> None:
        client = object.__new__(MiniTelnet)
        client.sock = DummySocket()
        client.pending = b""
        output = client._strip_telnet(bytes((IAC, WILL, 1)) + b"hello")
        self.assertEqual(output, b"hello")
        self.assertEqual(client.sock.sent, [bytes((IAC, DONT, 1))])

    def test_exec_discards_multiline_command_echo(self) -> None:
        client = EchoingCommandClient("")
        output, status = client.exec(
            "grep -E 'mmcblk0p(13|14)( |$)' /proc/mounts 2>/dev/null || true"
        )
        self.assertEqual(output, "")
        self.assertEqual(status, 0)

    def test_exec_preserves_only_real_command_output(self) -> None:
        client = EchoingCommandClient("real output")
        output, status = client.exec("printf 'real output'")
        self.assertEqual(output, "real output")
        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
