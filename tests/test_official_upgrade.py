from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from jdbox_athena.constants import (
    DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_MD5,
    DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SHA256,
    DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SIZE,
)
from jdbox_athena.errors import OfficialFirmwareUpgradeError, OperationCancelled
from jdbox_athena.official_upgrade import (
    OfficialFirmwareInfo,
    OfficialFirmwareUpgrader,
)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self, _limit: int = -1) -> bytes:
        return self.body


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.method = ""
        self.path = ""
        self.headers = {}
        self.sent = []
        self.closed = False

    def putrequest(self, method: str, path: str) -> None:
        self.method = method
        self.path = path

    def putheader(self, name: str, value: str) -> None:
        self.headers[name] = value

    def endheaders(self) -> None:
        return None

    def send(self, value: bytes) -> None:
        self.sent.append(value)

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class OfficialFirmwareUpgradeTests(unittest.TestCase):
    def make_upgrader(
        self,
        root: Path,
        image: Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> OfficialFirmwareUpgrader:
        return OfficialFirmwareUpgrader(
            "http://192.168.68.1/",
            image,
            root / "out",
            "root",
            "secret",
            cancel_event=cancel_event,
        )

    @staticmethod
    def image_info(path: Path, size: int) -> OfficialFirmwareInfo:
        return OfficialFirmwareInfo(
            path=str(path),
            filename=path.name,
            release="4.3.0.r4211",
            size_bytes=size,
            fit_size_bytes=max(size - 4, 8),
            md5=DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_MD5,
            sha256=DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SHA256,
        )

    def test_locked_r4211_size_structure_and_hash_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "r4211.img"
            with image.open("wb") as handle:
                handle.write(b"\xd0\x0d\xfe\xed")
                handle.write((DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SIZE - 540).to_bytes(4, "big"))
                handle.truncate(DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SIZE)
            upgrader = self.make_upgrader(root, image)
            with patch(
                "jdbox_athena.official_upgrade.hash_file",
                return_value=(
                    DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_MD5,
                    DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SHA256,
                ),
            ):
                info = upgrader.validate_image()
            self.assertEqual(info.release, "4.3.0.r4211")
            self.assertEqual(info.size_bytes, DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SIZE)

    def test_stock_upload_uses_expected_three_form_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "r4211.img"
            image.write_bytes(b"firmware-payload")
            info = self.image_info(image, image.stat().st_size)
            response = FakeResponse(
                json.dumps(
                    {"checksum": info.md5, "size": info.size_bytes}
                ).encode("utf-8")
            )
            connection = FakeConnection(response)
            upgrader = self.make_upgrader(root, image)
            upgrader.client.session = "test-session"
            with patch(
                "jdbox_athena.official_upgrade.http.client.HTTPConnection",
                return_value=connection,
            ):
                payload = upgrader._upload(info)
            wire_data = b"".join(connection.sent)
            self.assertEqual(payload["checksum"], info.md5)
            self.assertEqual(connection.path, "/cgi-bin/luci-upload")
            self.assertIn(b'name="sessionid"', wire_data)
            self.assertIn(b"test-session", wire_data)
            self.assertIn(b'name="filename"', wire_data)
            self.assertIn(b"/tmp/firmware.img", wire_data)
            self.assertIn(b'name="filedata"; filename="r4211.img"', wire_data)
            self.assertTrue(connection.closed)

    def test_cancelled_upload_never_contacts_router(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "r4211.img"
            image.write_bytes(b"payload")
            cancel_event = threading.Event()
            cancel_event.set()
            upgrader = self.make_upgrader(root, image, cancel_event=cancel_event)
            upgrader.client.session = "test-session"
            connection = Mock()
            with patch.object(
                upgrader, "_connection", return_value=(connection, "/upload")
            ), self.assertRaises(OperationCancelled):
                upgrader._upload(self.image_info(image, image.stat().st_size))
            connection.putrequest.assert_not_called()

    def test_prepare_requires_router_size_md5_and_firmware_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "r4211.img"
            image.write_bytes(b"payload")
            info = self.image_info(image, image.stat().st_size)
            upgrader = self.make_upgrader(root, image)
            with patch.object(upgrader, "validate_image", return_value=info), patch.object(
                upgrader.client, "login", return_value="session"
            ), patch.object(
                upgrader,
                "_upload",
                return_value={"checksum": info.md5, "size": info.size_bytes},
            ), patch.object(
                upgrader.client,
                "call_static",
                side_effect=(
                    {"type": "RE-CS-02", "version": "4.5.3.r4546"},
                    {"status": 0},
                ),
            ) as call_static:
                plan = upgrader.prepare()
            self.assertEqual(
                call_static.call_args_list,
                [
                    call("web_get_router_info", {}),
                    call("firmware_check", {}),
                ],
            )
            self.assertEqual(plan.device_type, "RE-CS-02")
            self.assertEqual(plan.current_release, "4.5.3.r4546")
            self.assertEqual(plan.firmware_check_status, 0)
            self.assertTrue(plan.confirmation_phrase.startswith("DOWNGRADE-R4211-"))

    def test_wrong_router_model_is_rejected_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "r4211.img"
            image.write_bytes(b"payload")
            info = self.image_info(image, image.stat().st_size)
            upgrader = self.make_upgrader(root, image)
            upload = Mock()
            with patch.object(upgrader, "validate_image", return_value=info), patch.object(
                upgrader.client, "login", return_value="session"
            ), patch.object(
                upgrader.client,
                "call_static",
                return_value={"type": "RE-CS-01", "version": "test"},
            ), patch.object(
                upgrader, "_upload", upload
            ), self.assertRaisesRegex(OfficialFirmwareUpgradeError, "不是 RE-CS-02"):
                upgrader.prepare()
            upload.assert_not_called()

    def test_commit_submits_local_upgrade_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "r4211.img"
            image.write_bytes(b"payload")
            info = self.image_info(image, image.stat().st_size)
            upgrader = self.make_upgrader(root, image)
            with patch.object(upgrader, "validate_image", return_value=info), patch.object(
                upgrader.client, "login", return_value="session"
            ), patch.object(
                upgrader,
                "_upload",
                return_value={"checksum": info.md5, "size": info.size_bytes},
            ), patch.object(
                upgrader.client,
                "call_static",
                side_effect=(
                    {"type": "RE-CS-02", "version": "4.5.3.r4546"},
                    {"status": 0},
                ),
            ):
                plan = upgrader.prepare()
            with patch.object(
                upgrader.client, "call_static", return_value={"status": 0}
            ) as action, patch.object(upgrader, "_wait_until_ready"):
                report_path = upgrader.commit_and_wait(plan)
            action.assert_called_once_with("local_upgrade_action", {})
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "complete")


if __name__ == "__main__":
    unittest.main()
