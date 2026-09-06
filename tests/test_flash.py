from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jdbox_athena.constants import (
    DEFAULT_UBOOT_IMAGE_MD5,
    DEFAULT_UBOOT_IMAGE_SHA256,
    DEFAULT_UBOOT_IMAGE_SIZE,
    UBOOT_ABORT_REPLY,
    UBOOT_ABORT_REQUEST,
)
from jdbox_athena.errors import FlashError
from jdbox_athena.flash import UbootFlasher
from jdbox_athena.models import Partition


class FakeShell:
    def run(self, command: str, **_kwargs: object) -> str:
        if "compatible" in command:
            return "jdcloud,re-cs-02\nqcom,ipq6018"
        return ""


class FlashSafetyTests(unittest.TestCase):
    def make_flasher(self, image: Path) -> UbootFlasher:
        return UbootFlasher(  # type: ignore[arg-type]
            FakeShell(),
            "192.168.68.4",
            image.parent,
            image,
        )

    def test_fixed_image_metadata_and_arm_header_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "uboot.bin"
            header = b"\x7fELF" + bytes((1, 1)) + b"\x00" * 12 + (40).to_bytes(2, "little")
            markers = UBOOT_ABORT_REQUEST + b"\x00" + UBOOT_ABORT_REPLY
            image.write_bytes(
                header + markers + b"\x00" * (DEFAULT_UBOOT_IMAGE_SIZE - len(header) - len(markers))
            )
            with patch(
                "jdbox_athena.flash.hash_file",
                return_value=(DEFAULT_UBOOT_IMAGE_MD5, DEFAULT_UBOOT_IMAGE_SHA256),
            ):
                info = self.make_flasher(image).validate_image()
            self.assertEqual(info.size_bytes, 655360)
            self.assertEqual(info.elf_machine, "ARM (EM_ARM=40)")
            self.assertTrue(info.network_abort_protocol)

    def test_wrong_image_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "uboot.bin"
            header = b"\x7fELF" + bytes((1, 1)) + b"\x00" * 12 + (40).to_bytes(2, "little")
            image.write_bytes(header + b"\x00" * (DEFAULT_UBOOT_IMAGE_SIZE - len(header)))
            with self.assertRaises(FlashError):
                self.make_flasher(image).validate_image()

    def test_dual_partition_identity_and_write_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            flasher = self.make_flasher(Path(directory) / "uboot.bin")
            partitions = {
                13: Partition(13, "mmcblk0p13", 655360, "0:APPSBL"),
                14: Partition(14, "mmcblk0p14", 655360, "0:APPSBL_1"),
            }
            compatible = flasher.validate_device(
                partitions,
                "JDCloud Technologies, Inc. IPQ6018/AP-CP03-C3",
                "DISTRIB_TARGET='ipq/ipq60xx'",
            )
            self.assertIn("re-cs-02", compatible)
            self.assertEqual(
                flasher.WRITE_ORDER,
                ("/dev/mmcblk0p14", "/dev/mmcblk0p13"),
            )

    def test_partition_label_mismatch_cannot_be_forced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            flasher = self.make_flasher(Path(directory) / "uboot.bin")
            partitions = {
                13: Partition(13, "mmcblk0p13", 655360, "wrong"),
                14: Partition(14, "mmcblk0p14", 655360, "0:APPSBL_1"),
            }
            with self.assertRaises(FlashError):
                flasher.validate_device(
                    partitions,
                    "JDCloud IPQ6018",
                    "DISTRIB_TARGET='ipq/ipq60xx'",
                )


if __name__ == "__main__":
    unittest.main()
