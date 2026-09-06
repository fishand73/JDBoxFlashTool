from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

from jdbox_athena.errors import PartitionResizeError
from jdbox_athena.partition_resize import (
    EXPECTED_PARTITION_LABELS,
    ROOTFS_SIZE_CHOICES_MIB,
    FullBackupInfo,
    RootfsResizer,
    generate_resized_gpt,
    parse_primary_gpt,
    validate_full_split_backup,
)

SECTOR = 512
ENTRY_COUNT = 28
ENTRY_SIZE = 128


def make_primary_gpt(*, rootfs_mib: int = 1, disk_mib: int = 16384) -> bytes:
    disk_sectors = disk_mib * 1024 * 1024 // SECTOR
    last_usable = disk_sectors - 34
    entries = bytearray(ENTRY_COUNT * ENTRY_SIZE)
    first = 34
    for number, label in EXPECTED_PARTITION_LABELS.items():
        if number == 15:
            sectors = 512 * 1024 // SECTOR
        elif number in (16, 17):
            sectors = 6 * 1024 * 1024 // SECTOR
        elif number == 18:
            sectors = rootfs_mib * 1024 * 1024 // SECTOR
        elif number == 27:
            sectors = last_usable - first + 1
        else:
            sectors = 1
        offset = (number - 1) * ENTRY_SIZE
        entries[offset : offset + 16] = bytes([number]) * 16
        entries[offset + 16 : offset + 32] = bytes([255 - number]) * 16
        struct.pack_into("<Q", entries, offset + 32, first)
        struct.pack_into("<Q", entries, offset + 40, first + sectors - 1)
        encoded = label.encode("utf-16le")
        entries[offset + 56 : offset + 56 + len(encoded)] = encoded
        first += sectors

    image = bytearray(34 * SECTOR)
    image[510:512] = b"\x55\xaa"
    image[2 * SECTOR : 2 * SECTOR + len(entries)] = entries
    header = bytearray(92)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, len(header))
    struct.pack_into("<Q", header, 24, 1)
    struct.pack_into("<Q", header, 32, disk_sectors - 1)
    struct.pack_into("<Q", header, 40, 34)
    struct.pack_into("<Q", header, 48, last_usable)
    header[56:72] = b"D" * 16
    struct.pack_into("<Q", header, 72, 2)
    struct.pack_into("<I", header, 80, ENTRY_COUNT)
    struct.pack_into("<I", header, 84, ENTRY_SIZE)
    struct.pack_into("<I", header, 88, zlib.crc32(entries) & 0xFFFFFFFF)
    struct.pack_into("<I", header, 16, zlib.crc32(header) & 0xFFFFFFFF)
    image[SECTOR : SECTOR + len(header)] = header
    return bytes(image)


def make_backup_gpt(primary_data: bytes) -> bytes:
    primary = parse_primary_gpt(primary_data)
    entries = primary_data[
        primary.entries_offset : primary.entries_offset + primary.entries_length
    ]
    image = bytearray(33 * SECTOR)
    image[: len(entries)] = entries
    header = bytearray(92)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, len(header))
    struct.pack_into("<Q", header, 24, primary.backup_lba)
    struct.pack_into("<Q", header, 32, 1)
    struct.pack_into("<Q", header, 40, primary.first_usable_lba)
    struct.pack_into("<Q", header, 48, primary.last_usable_lba)
    header[56:72] = primary_data[SECTOR + 56 : SECTOR + 72]
    struct.pack_into("<Q", header, 72, primary.backup_lba - 32)
    struct.pack_into("<I", header, 80, ENTRY_COUNT)
    struct.pack_into("<I", header, 84, ENTRY_SIZE)
    struct.pack_into("<I", header, 88, zlib.crc32(entries) & 0xFFFFFFFF)
    struct.pack_into("<I", header, 16, zlib.crc32(header) & 0xFFFFFFFF)
    image[32 * SECTOR : 32 * SECTOR + len(header)] = header
    return bytes(image)


def make_complete_backup(path: Path) -> None:
    primary = make_primary_gpt()
    parsed = parse_primary_gpt(primary)
    artifacts = []
    sums = []

    def add(filename: str, content: bytes, **metadata: object) -> None:
        (path / filename).write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        artifacts.append(
            {
                "filename": filename,
                "size_bytes": len(content),
                "sha256": digest,
                **metadata,
            }
        )
        sums.append(f"{digest}  {filename}\n")

    add(
        "gpt-primary.bin",
        primary,
        kind="gpt-primary",
        partition_number=None,
        partition_label=None,
    )
    add(
        "gpt-backup.bin",
        make_backup_gpt(primary),
        kind="gpt-backup",
        partition_number=None,
        partition_label=None,
    )
    for partition in parsed.partitions[:26]:
        add(
            f"p{partition.number:02d}.bin",
            bytes([partition.number]) * partition.size_bytes,
            kind="partition",
            partition_number=partition.number,
            partition_label=partition.label,
        )
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "read_only_backup": True,
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    (path / "SHA256SUMS").write_text("".join(sums), encoding="utf-8")


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self, _limit: int = -1) -> bytes:
        return self.body


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.path = ""
        self.sent = []
        self.closed = False

    def putrequest(self, _method: str, path: str) -> None:
        self.path = path

    def putheader(self, _name: str, _value: str) -> None:
        return None

    def endheaders(self) -> None:
        return None

    def send(self, value: bytes) -> None:
        self.sent.append(value)

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class PartitionResizeTests(unittest.TestCase):
    def test_fixed_choices_include_8_gib(self) -> None:
        self.assertEqual(ROOTFS_SIZE_CHOICES_MIB, (512, 1024, 2048, 8192))

    def test_generate_8_gib_preserves_p1_to_p17_and_shifts_later_entries(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as directory:
            root = Path(directory)
            backup_dir = root / "backup"
            backup_dir.mkdir()
            source = make_primary_gpt()
            (backup_dir / "gpt-primary.bin").write_bytes(source)
            backup = FullBackupInfo(str(backup_dir.resolve()), (), 1)
            generated = generate_resized_gpt(backup, root / "out", 8192)
            target = Path(generated.path).read_bytes()
            old = {p.number: p for p in parse_primary_gpt(source).partitions}
            new = {p.number: p for p in parse_primary_gpt(target).partitions}

            self.assertEqual(generated.rootfs_new_mib, 8192)
            for number in range(1, 18):
                offset = (number - 1) * ENTRY_SIZE + 2 * SECTOR
                self.assertEqual(
                    target[offset : offset + ENTRY_SIZE],
                    source[offset : offset + ENTRY_SIZE],
                )
            delta = new[18].sectors - old[18].sectors
            self.assertEqual(new[18].first_lba, old[18].first_lba)
            self.assertEqual(new[19].first_lba, old[19].first_lba + delta)
            self.assertEqual(new[27].first_lba, old[27].first_lba + delta)
            self.assertEqual(new[27].last_lba, old[27].last_lba)

    def test_bad_primary_header_crc_is_rejected(self) -> None:
        broken = bytearray(make_primary_gpt())
        broken[SECTOR + 24] ^= 1
        with self.assertRaisesRegex(PartitionResizeError, "CRC32"):
            parse_primary_gpt(bytes(broken))

    def test_complete_backup_hashes_every_partition(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as directory:
            root = Path(directory)
            make_complete_backup(root)
            info = validate_full_split_backup(root)
            self.assertEqual(len(info.verified_files), 28)
            (root / "p26.bin").write_bytes(b"damaged")
            with self.assertRaises(PartitionResizeError):
                validate_full_split_backup(root)

    def test_multipart_upload_uses_ptable_field(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as directory:
            root = Path(directory)
            image = root / "gpt.bin"
            image.write_bytes(b"gpt-payload")
            connection = FakeConnection(
                FakeResponse(b'{"status":"success","info":{}}')
            )
            resizer = RootfsResizer("http://192.168.1.1/", root, root, 512)
            with patch(
                "jdbox_athena.partition_resize.http.client.HTTPConnection",
                return_value=connection,
            ):
                payload = resizer._post_file(image)
            wire_data = b"".join(connection.sent)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(connection.path, "/upload")
            self.assertIn(b'name="ptable"; filename="gpt.bin"', wire_data)
            self.assertTrue(connection.closed)

    def test_confirmed_gpt_write_is_submitted_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as directory:
            root = Path(directory)
            backup_dir = root / "backup"
            backup_dir.mkdir()
            (backup_dir / "gpt-primary.bin").write_bytes(make_primary_gpt())
            backup = FullBackupInfo(str(backup_dir.resolve()), (), 1)
            resizer = RootfsResizer("http://192.168.1.1/", root / "out", backup_dir, 512)
            generated = generate_resized_gpt(backup, root / "out", 512)
            with patch.object(resizer, "probe_version", return_value="U-Boot test"), patch.object(
                resizer,
                "_post_file",
                return_value={
                    "status": "success",
                    "info": {
                        "type": "GPT (Single Image for eMMC device)",
                        "size": str(generated.size_bytes),
                        "md5": generated.md5,
                    },
                },
            ), patch.object(
                resizer,
                "_post_result",
                return_value={"status": "success", "info": {"reboot": False}},
            ) as result:
                report_path = resizer.commit(backup, generated, lambda _plan: True)
            result.assert_called_once_with()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "complete")

    def test_cancelled_gpt_write_never_posts_result(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as directory:
            root = Path(directory)
            backup_dir = root / "backup"
            backup_dir.mkdir()
            (backup_dir / "gpt-primary.bin").write_bytes(make_primary_gpt())
            backup = FullBackupInfo(str(backup_dir.resolve()), (), 1)
            resizer = RootfsResizer("http://192.168.1.1/", root / "out", backup_dir, 512)
            generated = generate_resized_gpt(backup, root / "out", 512)
            result = Mock()
            with patch.object(resizer, "probe_version", return_value="U-Boot test"), patch.object(
                resizer,
                "_post_file",
                return_value={
                    "status": "success",
                    "info": {
                        "type": "GPT (Single Image for eMMC device)",
                        "size": str(generated.size_bytes),
                        "md5": generated.md5,
                    },
                },
            ), patch.object(resizer, "_post_result", result), self.assertRaises(
                PartitionResizeError
            ):
                resizer.commit(backup, generated, lambda _plan: False)
            result.assert_not_called()


if __name__ == "__main__":
    unittest.main()
