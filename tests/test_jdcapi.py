from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from jdbox_athena.jdcapi import JdcApiClient


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class JdcApiTests(unittest.TestCase):
    def test_session_login_extracts_ubus_session(self) -> None:
        response = {"jsonrpc": "2.0", "id": 1, "result": [0, {"ubus_rpc_session": "abc"}]}
        with patch("urllib.request.urlopen", return_value=FakeResponse(response)):
            client = JdcApiClient("http://192.168.68.4/")
            self.assertEqual(client.login("root", "secret"), "abc")

    def test_r4546_strategy_uses_only_fixed_commands(self) -> None:
        client = JdcApiClient("http://192.168.68.4/")
        client.session = "session"
        calls = []

        def record(method: str, arguments: object) -> object:
            calls.append((method, arguments))
            return {}

        with patch.object(client, "call_static", side_effect=record), patch(
            "jdbox_athena.jdcapi.wait_for_tcp", return_value=True
        ):
            strategy = client.enable_telnet("192.168.68.4", 23, wait_seconds=0.1)
        self.assertEqual(strategy, "set_port_forward")
        self.assertEqual([call[0] for call in calls], ["set_port_forward", "set_port_forward"])
        payload_text = json.dumps([call[1] for call in calls])
        self.assertIn("factory_hm info telnet 1", payload_text)
        self.assertIn("telnetd", payload_text)

    def test_legacy_payload_is_available_as_fallback(self) -> None:
        client = JdcApiClient("http://192.168.68.4/")
        client.session = "session"
        calls = []
        with patch.object(
            client,
            "call_static",
            side_effect=lambda method, arguments: calls.append((method, arguments)) or {},
        ):
            client._attempt_commands("set_iptv_info")
        self.assertEqual([call[0] for call in calls], ["set_iptv_info", "set_iptv_info"])
        self.assertTrue(all("vid" in call[1] for call in calls))
        self.assertIn("telnetd -F -l /bin/login", json.dumps([call[1] for call in calls]))


if __name__ == "__main__":
    unittest.main()
