from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from jdbox_athena.constants import (
    DEFAULT_FACTORY_FIRMWARE_MD5,
    DEFAULT_FACTORY_FIRMWARE_SHA256,
    DEFAULT_FACTORY_FIRMWARE_SIZE,
    DEFAULT_FACTORY_KERNEL_SIZE,
)
from jdbox_athena.errors import FirmwareFlashError
from jdbox_athena.firmware_flash import (
    REQUIRED_PARTITION_LABELS,
    FactoryFirmwareInfo,
    FirmwareBackupInfo,
    FirmwareFlasher,
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


class FirmwareFlashTests(unittest.TestCase):
    def make_flasher(self, root: Path, image: Path, backup: Path) -> FirmwareFlasher:
        return FirmwareFlasher("http://192.168.1.1/", root / "out", image, backup)

    def make_sparse_factory(self, path: Path) -> None:
        with path.open("wb") as handle:
            handle.write(b"\xd0\x0d\xfe\xed")
            handle.write((DEFAULT_FACTORY_KERNEL_SIZE - 1024).to_bytes(4, "big"))
            handle.seek(512)
            handle.write(b"ARM64 OpenWrt jdcloud,re-cs-02")
            handle.seek(DEFAULT_FACTORY_KERNEL_SIZE)
            handle.write(b"hsqs")
            handle.truncate(DEFAULT_FACTORY_FIRMWARE_SIZE)

    def make_backup(self, path: Path) -> None:
        artifacts = []
        sums = []
        for kind in ("gpt-primary", "gpt-backup"):
            filename = f"{kind}.bin"
            content = kind.encode("ascii")
            (path / filename).write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            artifacts.append(
                {
                    "filename": filename,
                    "size_bytes": len(content),
                    "sha256": digest,
                    "kind": kind,
                    "partition_number": None,
                    "partition_label": None,
                }
            )
            sums.append(f"{digest}  {filename}\n")
        for number, label in REQUIRED_PARTITION_LABELS.items():
            filename = f"p{number:02d}.bin"
            content = f"partition-{number}".encode("ascii")
            (path / filename).write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            artifacts.append(
                {
                    "filename": filename,
                    "size_bytes": len(content),
                    "sha256": digest,
                    "kind": "partition",
                    "partition_number": number,
                    "partition_label": label,
                }
            )
            sums.append(f"{digest}  {filename}\n")
        (path / "manifest.json").write_text(
            json.dumps(
                {"schema_version": 1, "read_only_backup": True, "artifacts": artifacts}
            ),
            encoding="utf-8",
        )
        (path / "SHA256SUMS").write_text("".join(sums), encoding="utf-8")

    @staticmethod
    def locked_image_info(root: Path) -> FactoryFirmwareInfo:
        return FactoryFirmwareInfo(
            path=str(root / "factory.bin"),
            filename="factory.bin",
            size_bytes=DEFAULT_FACTORY_FIRMWARE_SIZE,
            kernel_size_bytes=DEFAULT_FACTORY_KERNEL_SIZE,
            rootfs_size_bytes=DEFAULT_FACTORY_FIRMWARE_SIZE
            - DEFAULT_FACTORY_KERNEL_SIZE,
            md5=DEFAULT_FACTORY_FIRMWARE_MD5,
            sha256=DEFAULT_FACTORY_FIRMWARE_SHA256,
            format="ARM64 OpenWrt FIT + SquashFS Factory",
            device="jdcloud_re-cs-02",
        )

    def patch_flash_preflight(
        self,
        stack: ExitStack,
        flasher: FirmwareFlasher,
        image: FactoryFirmwareInfo,
        backup: FirmwareBackupInfo,
        *,
        remote_md5: str | None = None,
    ) -> None:
        stack.enter_context(patch.object(flasher, "validate_image", return_value=image))
        stack.enter_context(patch.object(flasher, "validate_backup", return_value=backup))
        stack.enter_context(
            patch.object(flasher, "probe_version", return_value="U-Boot test")
        )
        stack.enter_context(
            patch.object(
                flasher,
                "_post_file",
                return_value={
                    "status": "success",
                    "info": {
                        "type": "FIT Image",
                        "size": str(image.size_bytes),
                        "md5": remote_md5 or image.md5,
                    },
                },
            )
        )

    def test_locked_factory_structure_and_hash_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "factory.bin"
            self.make_sparse_factory(image)
            flasher = self.make_flasher(root, image, root)
            with patch(
                "jdbox_athena.firmware_flash.hash_file",
                return_value=(DEFAULT_FACTORY_FIRMWARE_MD5, DEFAULT_FACTORY_FIRMWARE_SHA256),
            ):
                info = flasher.validate_image()
            self.assertEqual(info.kernel_size_bytes, 6 * 1024 * 1024)
            self.assertEqual(info.device, "jdcloud_re-cs-02")

    def test_backup_recovery_files_are_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_backup(root)
            info = self.make_flasher(root, root / "factory.bin", root).validate_backup()
            self.assertEqual(len(info.verified_files), len(REQUIRED_PARTITION_LABELS) + 2)

    def test_multipart_upload_uses_firmware_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "factory.bin"
            image.write_bytes(b"firmware-payload")
            response = FakeResponse(b'{"status":"success","info":{}}')
            connection = FakeConnection(response)
            flasher = self.make_flasher(root, image, root)
            with patch(
                "jdbox_athena.firmware_flash.http.client.HTTPConnection",
                return_value=connection,
            ):
                payload = flasher._post_file("firmware", image)
            wire_data = b"".join(connection.sent)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(connection.path, "/upload")
            self.assertIn(b'name="firmware"; filename="factory.bin"', wire_data)
            self.assertIn(b"firmware-payload", wire_data)
            self.assertTrue(connection.closed)

    def test_cancel_after_remote_validation_never_posts_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flasher = self.make_flasher(root, root / "factory.bin", root)
            image = self.locked_image_info(root)
            backup = FirmwareBackupInfo(str(root), ("p16.bin", "p18.bin"), 1)
            result = Mock()
            with ExitStack() as stack:
                self.patch_flash_preflight(stack, flasher, image, backup)
                stack.enter_context(patch.object(flasher, "_post_result", result))
                stack.enter_context(self.assertRaises(FirmwareFlashError))
                flasher.flash(lambda _plan: False)
            result.assert_not_called()
            report = json.loads(flasher.report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "cancelled-before-write")

    def test_remote_md5_mismatch_never_posts_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flasher = self.make_flasher(root, root / "factory.bin", root)
            image = self.locked_image_info(root)
            backup = FirmwareBackupInfo(str(root), ("p16.bin", "p18.bin"), 1)
            result = Mock()
            with ExitStack() as stack:
                self.patch_flash_preflight(
                    stack, flasher, image, backup, remote_md5="0" * 32
                )
                stack.enter_context(patch.object(flasher, "_post_result", result))
                stack.enter_context(self.assertRaises(FirmwareFlashError))
                flasher.flash(lambda _plan: True)
            result.assert_not_called()

    def test_confirmed_write_is_submitted_once_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flasher = self.make_flasher(root, root / "factory.bin", root)
            image = self.locked_image_info(root)
            backup = FirmwareBackupInfo(str(root), ("p16.bin", "p18.bin"), 1)
            with ExitStack() as stack:
                self.patch_flash_preflight(stack, flasher, image, backup)
                result = stack.enter_context(
                    patch.object(
                        flasher,
                        "_post_result",
                        return_value={
                            "status": "success",
                            "info": {"reboot": False},
                        },
                    )
                )
                report_path = flasher.flash(lambda _plan: True)
            result.assert_called_once_with(False)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "complete")


if __name__ == "__main__":
    unittest.main()
