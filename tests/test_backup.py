from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jdbox_athena.backup import AthenaBackupRunner, BackupOptions
from jdbox_athena.errors import RpcError


class BackupTelnetRecoveryTests(unittest.TestCase):
    @staticmethod
    def options(output: Path) -> BackupOptions:
        return BackupOptions(
            management_url="http://192.168.68.1/",
            router_host="192.168.68.1",
            telnet_port=23,
            username="root",
            password="secret",
            operation="backup",
            mode="split",
            output=output,
            uboot_image=None,
            remote_target=None,
            http_port=18080,
            pc_host=None,
            listen_host="0.0.0.0",
            stream_port=0,
            force_device=False,
            rpc_timeout=1.0,
            telnet_wait=0.1,
            command_timeout=1.0,
            raw_connect_timeout=1.0,
        )

    def test_recovery_runs_only_after_login_and_both_strategies_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Mock()
            second = Mock()
            failure = RpcError("both strategies failed")
            first.enable_telnet.side_effect = failure
            second.enable_telnet.return_value = "set_iptv_info"
            recover = Mock()
            runner = AthenaBackupRunner(self.options(Path(directory)), recover_telnet=recover)
            with patch("jdbox_athena.backup.is_tcp_open", return_value=False), patch(
                "jdbox_athena.backup.JdcApiClient", side_effect=(first, second)
            ):
                runner.ensure_telnet()
            recover.assert_called_once_with(failure)
            first.login.assert_called_once_with("root", "secret")
            second.login.assert_called_once_with("root", "secret")
            self.assertEqual(runner.telnet_strategy, "r4211-recovery+set_iptv_info")

    def test_login_failure_never_triggers_firmware_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            client.login.side_effect = RpcError("bad password")
            recover = Mock()
            runner = AthenaBackupRunner(self.options(Path(directory)), recover_telnet=recover)
            with patch("jdbox_athena.backup.is_tcp_open", return_value=False), patch(
                "jdbox_athena.backup.JdcApiClient", return_value=client
            ), self.assertRaises(RpcError):
                runner.ensure_telnet()
            recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
