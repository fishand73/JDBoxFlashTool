from __future__ import annotations

import unittest

from jdbox_athena.cli import create_parser
from jdbox_athena.constants import DEFAULT_MANAGEMENT_URL, RAW_PREFIX_MIB
from jdbox_athena.errors import AthenaError
from jdbox_athena.util import normalize_management_url, safe_filename


class CliTests(unittest.TestCase):
    def test_defaults_are_safe_split_mode(self) -> None:
        args = create_parser().parse_args([])
        self.assertEqual(args.management_url, DEFAULT_MANAGEMENT_URL)
        self.assertEqual(DEFAULT_MANAGEMENT_URL, "http://192.168.68.1/")
        self.assertEqual(args.user, "root")
        self.assertIsNone(args.password)
        self.assertEqual(args.mode, "split")
        self.assertFalse(args.force_device)

    def test_raw_size_is_fixed(self) -> None:
        self.assertEqual(RAW_PREFIX_MIB, 2555)

    def test_flash_is_an_explicit_mutually_exclusive_operation(self) -> None:
        parser = create_parser()
        args = parser.parse_args(["--flash-uboot"])
        self.assertTrue(args.flash_uboot)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--flash-uboot", "--mode", "raw"])

    def test_uboot_enter_accepts_interface_index(self) -> None:
        args = create_parser().parse_args(["--enter-uboot", "7", "--no-open-browser"])
        self.assertEqual(args.enter_uboot, "7")
        self.assertTrue(args.no_open_browser)

    def test_firmware_flash_is_explicit_and_mutually_exclusive(self) -> None:
        parser = create_parser()
        args = parser.parse_args(["--flash-firmware", "7", "--firmware-reboot"])
        self.assertEqual(args.flash_firmware, "7")
        self.assertTrue(args.firmware_reboot)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--flash-firmware", "--flash-uboot"])

    def test_rootfs_resize_has_four_fixed_sizes(self) -> None:
        parser = create_parser()
        for size in (512, 1024, 2048, 8192):
            args = parser.parse_args(["--resize-rootfs", str(size)])
            self.assertEqual(args.resize_rootfs, size)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--resize-rootfs", "4096"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--resize-rootfs", "1024", "--flash-firmware"])

    def test_bare_host_is_normalized(self) -> None:
        url, host = normalize_management_url("192.168.68.1")
        self.assertEqual(url, DEFAULT_MANAGEMENT_URL)
        self.assertEqual(host, "192.168.68.1")

    def test_non_http_management_url_is_rejected(self) -> None:
        with self.assertRaises(AthenaError):
            normalize_management_url("ftp://192.168.68.4")

    def test_partition_label_becomes_portable_filename(self) -> None:
        self.assertEqual(safe_filename('0:ART / test*'), "0_ART_test")


if __name__ == "__main__":
    unittest.main()
