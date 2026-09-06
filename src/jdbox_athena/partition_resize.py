"""Device-specific GPT generation and guarded rootfs expansion through U-Boot Web."""

from __future__ import annotations

import http.client
import json
import logging
import secrets
import struct
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from .errors import PartitionResizeError
from .firmware_flash import normalize_uboot_web_url
from .integrity import hash_file

LOGGER = logging.getLogger(__name__)

SECTOR_SIZE = 512
GPT_HEADER_LBA = 1
GPT_SIGNATURE = b"EFI PART"
GPT_MIN_HEADER_SIZE = 92
PRIMARY_GPT_IMAGE_SECTORS = 34
BACKUP_GPT_IMAGE_SECTORS = 33
HTTP_RESPONSE_LIMIT = 64 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024
ROOTFS_PARTITION = 18
FIRST_SHIFTED_PARTITION = 19
STORAGE_PARTITION = 27
ROOTFS_SIZE_CHOICES_MIB = (512, 1024, 2048, 8192)
MIN_STORAGE_AFTER_RESIZE_MIB = 1024

EXPECTED_PARTITION_LABELS = {
    1: "0:SBL1",
    2: "0:BOOTCONFIG",
    3: "0:BOOTCONFIG1",
    4: "0:QSEE",
    5: "0:QSEE_1",
    6: "0:DEVCFG",
    7: "0:DEVCFG_1",
    8: "0:RPM",
    9: "0:RPM_1",
    10: "0:CDT",
    11: "0:CDT_1",
    12: "0:APPSBLENV",
    13: "0:APPSBL",
    14: "0:APPSBL_1",
    15: "0:ART",
    16: "0:HLOS",
    17: "0:HLOS_1",
    18: "rootfs",
    19: "0:WIFIFW",
    20: "rootfs_1",
    21: "0:WIFIFW_1",
    22: "rootfs_data",
    23: "0:ETHPHYFW",
    24: "plugin",
    25: "log",
    26: "swap",
    27: "storage",
}


@dataclass(frozen=True)
class GptPartition:
    """One allocated GPT partition entry."""

    number: int
    label: str
    first_lba: int
    last_lba: int
    entry_offset: int

    @property
    def sectors(self) -> int:
        return self.last_lba - self.first_lba + 1

    @property
    def size_bytes(self) -> int:
        return self.sectors * SECTOR_SIZE


@dataclass(frozen=True)
class ParsedPrimaryGpt:
    """Validated primary GPT metadata needed to build a replacement."""

    data: bytes
    header_size: int
    backup_lba: int
    first_usable_lba: int
    last_usable_lba: int
    entry_lba: int
    entry_count: int
    entry_size: int
    entries_offset: int
    entries_length: int
    partitions: Tuple[GptPartition, ...]


@dataclass(frozen=True)
class FullBackupInfo:
    """A complete, hash-verified split backup used as the GPT source."""

    path: str
    verified_files: Tuple[str, ...]
    schema_version: int


@dataclass(frozen=True)
class PartitionChange:
    """The before/after geometry shown in the high-risk confirmation."""

    number: int
    label: str
    old_first_lba: int
    old_last_lba: int
    new_first_lba: int
    new_last_lba: int
    old_size_mib: float
    new_size_mib: float


@dataclass(frozen=True)
class GeneratedGptInfo:
    """A device-bound GPT image generated from a verified backup."""

    path: str
    filename: str
    size_bytes: int
    md5: str
    sha256: str
    disk_size_bytes: int
    rootfs_old_mib: int
    rootfs_new_mib: int
    storage_old_mib: float
    storage_new_mib: float
    shifted_sectors: int
    changes: Tuple[PartitionChange, ...]


@dataclass(frozen=True)
class RootfsResizePlan:
    """Remote-validated GPT write details bound to a confirmation phrase."""

    generated_gpt: GeneratedGptInfo
    backup: FullBackupInfo
    web_url: str
    uboot_version: str
    upload_info: Mapping[str, Any]
    confirmation_phrase: str


ResizeConfirmationCallback = Callable[[RootfsResizePlan], bool]


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _mib(size_bytes: int) -> float:
    return size_bytes / (1024 * 1024)


def parse_primary_gpt(data: bytes) -> ParsedPrimaryGpt:
    """Parse and checksum a primary GPT image beginning at disk LBA 0."""

    expected_length = PRIMARY_GPT_IMAGE_SECTORS * SECTOR_SIZE
    if len(data) != expected_length:
        raise PartitionResizeError(
            f"主 GPT 备份大小异常：{len(data)} != {expected_length} bytes。"
        )
    if data[510:512] != b"\x55\xaa":
        raise PartitionResizeError("主 GPT 备份缺少有效的保护 MBR 签名。")
    header_offset = GPT_HEADER_LBA * SECTOR_SIZE
    if data[header_offset : header_offset + 8] != GPT_SIGNATURE:
        raise PartitionResizeError("主 GPT 备份缺少 EFI PART 签名。")

    header_size = struct.unpack_from("<I", data, header_offset + 12)[0]
    if header_size < GPT_MIN_HEADER_SIZE or header_size > SECTOR_SIZE:
        raise PartitionResizeError(f"GPT 头长度异常：{header_size} bytes。")
    stored_header_crc = struct.unpack_from("<I", data, header_offset + 16)[0]
    header = bytearray(data[header_offset : header_offset + header_size])
    struct.pack_into("<I", header, 16, 0)
    if _crc32(header) != stored_header_crc:
        raise PartitionResizeError("主 GPT 头 CRC32 校验失败，禁止生成新分区表。")

    current_lba = struct.unpack_from("<Q", data, header_offset + 24)[0]
    backup_lba = struct.unpack_from("<Q", data, header_offset + 32)[0]
    first_usable = struct.unpack_from("<Q", data, header_offset + 40)[0]
    last_usable = struct.unpack_from("<Q", data, header_offset + 48)[0]
    entry_lba = struct.unpack_from("<Q", data, header_offset + 72)[0]
    entry_count = struct.unpack_from("<I", data, header_offset + 80)[0]
    entry_size = struct.unpack_from("<I", data, header_offset + 84)[0]
    stored_entries_crc = struct.unpack_from("<I", data, header_offset + 88)[0]
    if current_lba != GPT_HEADER_LBA or entry_lba != 2:
        raise PartitionResizeError("只支持从磁盘 LBA 0 开始的标准主 GPT 备份。")
    if backup_lba <= last_usable or first_usable >= last_usable:
        raise PartitionResizeError("GPT 声明的磁盘可用范围异常。")
    if entry_count not in (28, 128) or entry_size != 128:
        raise PartitionResizeError(
            f"GPT 分区项布局异常：count={entry_count}, size={entry_size}。"
        )

    entries_offset = entry_lba * SECTOR_SIZE
    entries_length = entry_count * entry_size
    entries_end = entries_offset + entries_length
    if entries_end > len(data):
        raise PartitionResizeError("主 GPT 备份没有包含完整的分区项数组。")
    entries = data[entries_offset:entries_end]
    if _crc32(entries) != stored_entries_crc:
        raise PartitionResizeError("主 GPT 分区项 CRC32 校验失败，禁止生成新分区表。")

    partitions: List[GptPartition] = []
    for index in range(entry_count):
        offset = entries_offset + index * entry_size
        if data[offset : offset + 16] == b"\0" * 16:
            continue
        first_lba = struct.unpack_from("<Q", data, offset + 32)[0]
        last_lba = struct.unpack_from("<Q", data, offset + 40)[0]
        raw_label = data[offset + 56 : offset + min(entry_size, 128)]
        label = raw_label.decode("utf-16le", errors="strict").split("\0", 1)[0]
        number = index + 1
        if not label or first_lba < first_usable or last_lba > last_usable:
            raise PartitionResizeError(f"GPT 分区 p{number} 的标签或范围异常。")
        if first_lba > last_lba:
            raise PartitionResizeError(f"GPT 分区 p{number} 的起止 LBA 颠倒。")
        partitions.append(GptPartition(number, label, first_lba, last_lba, offset))

    by_number = {partition.number: partition for partition in partitions}
    for number, expected in EXPECTED_PARTITION_LABELS.items():
        partition = by_number.get(number)
        if partition is None:
            raise PartitionResizeError(f"GPT 缺少雅典娜必需分区 p{number}。")
        if partition.label != expected:
            raise PartitionResizeError(
                f"GPT p{number} 标签不符：{partition.label!r} != {expected!r}。"
            )
    if len(partitions) != STORAGE_PARTITION:
        raise PartitionResizeError(
            f"检测到 {len(partitions)} 个已分配分区，不是预期的 {STORAGE_PARTITION} 个。"
        )
    for number in range(ROOTFS_PARTITION, STORAGE_PARTITION):
        left = by_number[number]
        right = by_number[number + 1]
        if left.last_lba + 1 != right.first_lba:
            raise PartitionResizeError(
                f"p{number} 与 p{number + 1} 之间不是连续布局，禁止自动顺移。"
            )
    if by_number[STORAGE_PARTITION].last_lba != last_usable:
        raise PartitionResizeError("storage 分区没有延伸到 GPT 最后可用 LBA，禁止自动调整。")
    if by_number[15].size_bytes != 512 * 1024:
        raise PartitionResizeError("ART 分区不是本工具支持的 512 KiB 雅典娜布局。")
    if by_number[16].size_bytes != 6 * 1024 * 1024:
        raise PartitionResizeError("0:HLOS 分区不是本工具支持的 6 MiB 布局。")
    if by_number[17].size_bytes != 6 * 1024 * 1024:
        raise PartitionResizeError("0:HLOS_1 分区不是本工具支持的 6 MiB 布局。")

    return ParsedPrimaryGpt(
        data=data,
        header_size=header_size,
        backup_lba=backup_lba,
        first_usable_lba=first_usable,
        last_usable_lba=last_usable,
        entry_lba=entry_lba,
        entry_count=entry_count,
        entry_size=entry_size,
        entries_offset=entries_offset,
        entries_length=entries_length,
        partitions=tuple(partitions),
    )


def validate_backup_gpt(data: bytes, primary: ParsedPrimaryGpt) -> None:
    """Validate the tail GPT backup and ensure it describes the same disk."""

    expected_length = BACKUP_GPT_IMAGE_SECTORS * SECTOR_SIZE
    if len(data) != expected_length:
        raise PartitionResizeError(
            f"备用 GPT 备份大小异常：{len(data)} != {expected_length} bytes。"
        )
    header_offset = (BACKUP_GPT_IMAGE_SECTORS - 1) * SECTOR_SIZE
    if data[header_offset : header_offset + 8] != GPT_SIGNATURE:
        raise PartitionResizeError("备用 GPT 备份缺少 EFI PART 签名。")
    header_size = struct.unpack_from("<I", data, header_offset + 12)[0]
    if header_size < GPT_MIN_HEADER_SIZE or header_size > SECTOR_SIZE:
        raise PartitionResizeError(f"备用 GPT 头长度异常：{header_size} bytes。")
    stored_header_crc = struct.unpack_from("<I", data, header_offset + 16)[0]
    header = bytearray(data[header_offset : header_offset + header_size])
    struct.pack_into("<I", header, 16, 0)
    if _crc32(header) != stored_header_crc:
        raise PartitionResizeError("备用 GPT 头 CRC32 校验失败。")

    current_lba = struct.unpack_from("<Q", data, header_offset + 24)[0]
    primary_lba = struct.unpack_from("<Q", data, header_offset + 32)[0]
    first_usable = struct.unpack_from("<Q", data, header_offset + 40)[0]
    last_usable = struct.unpack_from("<Q", data, header_offset + 48)[0]
    entry_lba = struct.unpack_from("<Q", data, header_offset + 72)[0]
    entry_count = struct.unpack_from("<I", data, header_offset + 80)[0]
    entry_size = struct.unpack_from("<I", data, header_offset + 84)[0]
    stored_entries_crc = struct.unpack_from("<I", data, header_offset + 88)[0]
    capture_first_lba = primary.backup_lba - (BACKUP_GPT_IMAGE_SECTORS - 1)
    expected_entry_lba = capture_first_lba
    if (
        current_lba != primary.backup_lba
        or primary_lba != GPT_HEADER_LBA
        or first_usable != primary.first_usable_lba
        or last_usable != primary.last_usable_lba
        or entry_lba != expected_entry_lba
        or entry_count != primary.entry_count
        or entry_size != primary.entry_size
    ):
        raise PartitionResizeError("主、备用 GPT 声明的磁盘几何或分区项布局不一致。")
    entries = data[: primary.entries_length]
    if _crc32(entries) != stored_entries_crc:
        raise PartitionResizeError("备用 GPT 分区项 CRC32 校验失败。")
    primary_entries = primary.data[
        primary.entries_offset : primary.entries_offset + primary.entries_length
    ]
    if entries != primary_entries:
        raise PartitionResizeError("主、备用 GPT 的分区项内容不一致。")
    primary_guid = primary.data[SECTOR_SIZE + 56 : SECTOR_SIZE + 72]
    backup_guid = data[header_offset + 56 : header_offset + 72]
    if backup_guid != primary_guid:
        raise PartitionResizeError("主、备用 GPT 的磁盘 GUID 不一致。")


def _load_sha256s(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            values[parts[1].lstrip("*")] = parts[0].lower()
    return values


def validate_full_split_backup(backup_dir: Path) -> FullBackupInfo:
    """Hash every GPT and p1-p26 artifact before permitting a GPT write."""

    backup = backup_dir.expanduser().resolve()
    manifest_path = backup / "manifest.json"
    sums_path = backup / "SHA256SUMS"
    if not manifest_path.is_file() or not sums_path.is_file():
        raise PartitionResizeError("完整备份缺少 manifest.json 或 SHA256SUMS。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PartitionResizeError(f"无法读取备份 manifest：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("read_only_backup") is not True:
        raise PartitionResizeError("备份不是本工具生成的只读 split 备份格式。")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise PartitionResizeError("备份 manifest 缺少 artifacts。")

    by_kind: Dict[str, Mapping[str, Any]] = {}
    by_partition: Dict[int, Mapping[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        kind = artifact.get("kind")
        number = artifact.get("partition_number")
        if isinstance(kind, str):
            by_kind[kind] = artifact
        if isinstance(number, int):
            by_partition[number] = artifact

    required: List[Mapping[str, Any]] = []
    for kind in ("gpt-primary", "gpt-backup"):
        artifact = by_kind.get(kind)
        if artifact is None:
            raise PartitionResizeError(f"完整备份缺少 {kind}。")
        required.append(artifact)
    for number in range(1, STORAGE_PARTITION):
        artifact = by_partition.get(number)
        if artifact is None:
            raise PartitionResizeError(f"完整备份缺少 p{number}。")
        expected_label = EXPECTED_PARTITION_LABELS[number]
        if artifact.get("partition_label") != expected_label:
            raise PartitionResizeError(
                f"备份 p{number} 标签不符：{artifact.get('partition_label')!r} "
                f"!= {expected_label!r}。"
            )
        required.append(artifact)

    sums = _load_sha256s(sums_path)
    verified: List[str] = []
    LOGGER.info("完整校验 GPT 与 p1-p26 恢复备份：%s", backup)
    for artifact in required:
        filename = artifact.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise PartitionResizeError("备份 manifest 包含无效文件名。")
        path = backup / filename
        if not path.is_file():
            raise PartitionResizeError(f"备份文件缺失：{filename}")
        expected_size = artifact.get("size_bytes")
        if not isinstance(expected_size, int) or path.stat().st_size != expected_size:
            raise PartitionResizeError(f"备份文件大小不符：{filename}")
        expected_sha = artifact.get("sha256")
        if not isinstance(expected_sha, str) or sums.get(filename) != expected_sha.lower():
            raise PartitionResizeError(f"备份 SHA256 清单不一致：{filename}")
        _md5, actual_sha = hash_file(path)
        if actual_sha.lower() != expected_sha.lower():
            raise PartitionResizeError(f"备份文件已损坏：{filename}")
        verified.append(filename)

    primary_artifact = by_kind["gpt-primary"]
    primary_path = backup / str(primary_artifact["filename"])
    parsed = parse_primary_gpt(primary_path.read_bytes())
    backup_artifact = by_kind["gpt-backup"]
    backup_gpt_path = backup / str(backup_artifact["filename"])
    validate_backup_gpt(backup_gpt_path.read_bytes(), parsed)
    partitions = {partition.number: partition for partition in parsed.partitions}
    for number in range(1, STORAGE_PARTITION):
        declared_size = by_partition[number].get("size_bytes")
        if declared_size != partitions[number].size_bytes:
            raise PartitionResizeError(
                f"备份 p{number} 文件大小与 GPT 分区大小不一致，禁止继续。"
            )
    schema = manifest.get("schema_version")
    return FullBackupInfo(
        path=str(backup),
        verified_files=tuple(verified),
        schema_version=schema if isinstance(schema, int) else 0,
    )


def generate_resized_gpt(
    backup: FullBackupInfo,
    output: Path,
    target_rootfs_mib: int,
) -> GeneratedGptInfo:
    """Generate one primary GPT while preserving all identity fields and p1-p17."""

    if target_rootfs_mib not in ROOTFS_SIZE_CHOICES_MIB:
        choices = ", ".join(str(value) for value in ROOTFS_SIZE_CHOICES_MIB)
        raise PartitionResizeError(f"rootfs 只能选择 {choices} MiB。")
    backup_path = Path(backup.path)
    source_path = backup_path / "gpt-primary.bin"
    parsed = parse_primary_gpt(source_path.read_bytes())
    partitions = {partition.number: partition for partition in parsed.partitions}
    rootfs = partitions[ROOTFS_PARTITION]
    storage = partitions[STORAGE_PARTITION]
    target_sectors = target_rootfs_mib * 1024 * 1024 // SECTOR_SIZE
    delta = target_sectors - rootfs.sectors
    if delta == 0:
        raise PartitionResizeError(f"rootfs 已经是 {target_rootfs_mib} MiB，无需重复写 GPT。")
    if delta < 0:
        old_mib = rootfs.size_bytes // 1024 // 1024
        raise PartitionResizeError(
            f"rootfs 当前为 {old_mib} MiB；为避免截断数据，本工具不支持缩小。"
        )
    storage_new_sectors = storage.sectors - delta
    minimum_storage_sectors = MIN_STORAGE_AFTER_RESIZE_MIB * 1024 * 1024 // SECTOR_SIZE
    if storage_new_sectors < minimum_storage_sectors:
        raise PartitionResizeError(
            f"扩容后 storage 小于安全下限 {MIN_STORAGE_AFTER_RESIZE_MIB} MiB，禁止生成。"
        )

    updated = bytearray(parsed.data)
    changes: List[PartitionChange] = []
    for number in range(ROOTFS_PARTITION, STORAGE_PARTITION + 1):
        partition = partitions[number]
        if number == ROOTFS_PARTITION:
            new_first = partition.first_lba
            new_last = partition.last_lba + delta
        elif number == STORAGE_PARTITION:
            new_first = partition.first_lba + delta
            new_last = partition.last_lba
        else:
            new_first = partition.first_lba + delta
            new_last = partition.last_lba + delta
        struct.pack_into("<Q", updated, partition.entry_offset + 32, new_first)
        struct.pack_into("<Q", updated, partition.entry_offset + 40, new_last)
        changes.append(
            PartitionChange(
                number=number,
                label=partition.label,
                old_first_lba=partition.first_lba,
                old_last_lba=partition.last_lba,
                new_first_lba=new_first,
                new_last_lba=new_last,
                old_size_mib=_mib(partition.size_bytes),
                new_size_mib=_mib((new_last - new_first + 1) * SECTOR_SIZE),
            )
        )

    entries = bytes(
        updated[
            parsed.entries_offset : parsed.entries_offset + parsed.entries_length
        ]
    )
    header_offset = GPT_HEADER_LBA * SECTOR_SIZE
    struct.pack_into("<I", updated, header_offset + 88, _crc32(entries))
    struct.pack_into("<I", updated, header_offset + 16, 0)
    header = bytes(updated[header_offset : header_offset + parsed.header_size])
    struct.pack_into("<I", updated, header_offset + 16, _crc32(header))

    regenerated = parse_primary_gpt(bytes(updated))
    regenerated_partitions = {part.number: part for part in regenerated.partitions}
    for number in range(1, ROOTFS_PARTITION):
        before = partitions[number]
        after = regenerated_partitions[number]
        if (before.first_lba, before.last_lba) != (after.first_lba, after.last_lba):
            raise PartitionResizeError(f"内部校验失败：p{number} 被意外修改。")

    output.mkdir(parents=True, exist_ok=True)
    path = output / f"gpt-rootfs-{target_rootfs_mib}MiB.bin"
    path.write_bytes(updated)
    md5, sha256 = hash_file(path)
    return GeneratedGptInfo(
        path=str(path.resolve()),
        filename=path.name,
        size_bytes=path.stat().st_size,
        md5=md5.lower(),
        sha256=sha256.lower(),
        disk_size_bytes=(parsed.backup_lba + 1) * SECTOR_SIZE,
        rootfs_old_mib=rootfs.size_bytes // 1024 // 1024,
        rootfs_new_mib=target_rootfs_mib,
        storage_old_mib=_mib(storage.size_bytes),
        storage_new_mib=_mib(storage_new_sectors * SECTOR_SIZE),
        shifted_sectors=delta,
        changes=tuple(changes),
    )


class RootfsResizer:
    """Generate locally, validate again in U-Boot RAM, then commit one GPT write."""

    def __init__(
        self,
        web_url: str,
        output: Path,
        backup_dir: Path,
        target_rootfs_mib: int,
        *,
        timeout: float = 600.0,
    ) -> None:
        self.web_url = normalize_uboot_web_url(web_url)
        self.output = output
        self.backup_dir = backup_dir
        self.target_rootfs_mib = target_rootfs_mib
        self.timeout = timeout
        self.report_path = output / "rootfs-resize-report.json"
        self.report: Dict[str, Any] = {
            "schema_version": 1,
            "operation": "resize-rootfs",
            "status": "created",
            "target_rootfs_mib": target_rootfs_mib,
        }

    def _write_report(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def prepare(self) -> Tuple[FullBackupInfo, GeneratedGptInfo]:
        """Verify all recovery files and create a device-bound replacement GPT."""

        backup = validate_full_split_backup(self.backup_dir)
        generated = generate_resized_gpt(backup, self.output, self.target_rootfs_mib)
        self.report.update(
            {
                "status": "local-preflight-complete",
                "backup": asdict(backup),
                "generated_gpt": asdict(generated),
                "web_url": self.web_url,
            }
        )
        self._write_report()
        return backup, generated

    def set_web_url(self, value: str) -> None:
        """Switch to the URL discovered by uBootEnter without regenerating the GPT."""

        self.web_url = normalize_uboot_web_url(value)
        self.report["web_url"] = self.web_url
        self._write_report()

    def _connection(self) -> http.client.HTTPConnection:
        parsed = urlparse(self.web_url)
        if parsed.hostname is None:
            raise PartitionResizeError("U-Boot Web 地址缺少主机名。")
        return http.client.HTTPConnection(
            parsed.hostname,
            parsed.port or 80,
            timeout=self.timeout,
        )

    def probe_version(self, *, required: bool = True) -> Optional[str]:
        """Confirm that the endpoint is the compatible U-Boot Web server."""

        connection = self._connection()
        try:
            connection.request("GET", "/version", headers={"Connection": "close"})
            response = connection.getresponse()
            body = response.read(4096).decode("utf-8", errors="replace").strip()
        except (OSError, http.client.HTTPException) as exc:
            if required:
                raise PartitionResizeError(
                    f"无法连接 U-Boot Web {self.web_url}：{exc}"
                ) from exc
            return None
        finally:
            connection.close()
        if response.status == 200 and body.startswith("U-Boot"):
            return body
        if required:
            raise PartitionResizeError(
                f"{self.web_url} 不是兼容的 U-Boot Web（HTTP {response.status}）。"
            )
        return None

    @staticmethod
    def _decode_json(status: int, body: bytes, action: str) -> Dict[str, Any]:
        text = body.decode("utf-8", errors="replace").strip()
        if status != 200:
            raise PartitionResizeError(f"U-Boot {action}返回 HTTP {status}：{text[:500]}")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise PartitionResizeError(
                f"U-Boot {action}返回的不是 JSON：{text[:500]}"
            ) from exc
        if not isinstance(payload, dict):
            raise PartitionResizeError(f"U-Boot {action}返回格式异常。")
        return payload

    def _post_file(self, path: Path) -> Dict[str, Any]:
        boundary = "----JDBoxAthena" + secrets.token_hex(12)
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="ptable"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        total = len(prefix) + path.stat().st_size + len(suffix)
        connection = self._connection()
        try:
            connection.putrequest("POST", "/upload")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(total))
            connection.putheader("Connection", "close")
            connection.endheaders()
            connection.send(prefix)
            with path.open("rb") as handle:
                while True:
                    block = handle.read(UPLOAD_CHUNK_SIZE)
                    if not block:
                        break
                    connection.send(block)
            connection.send(suffix)
            response = connection.getresponse()
            body = response.read(HTTP_RESPONSE_LIMIT)
            return self._decode_json(response.status, body, "GPT 上传校验")
        except (OSError, http.client.HTTPException) as exc:
            raise PartitionResizeError(
                f"上传 GPT 失败（尚未写入闪存）：{exc}"
            ) from exc
        finally:
            connection.close()

    def _post_result(self) -> Dict[str, Any]:
        boundary = "----JDBoxAthena" + secrets.token_hex(12)
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="auto_reboot"\r\n\r\n'
            "false\r\n"
            f"--{boundary}--\r\n"
        ).encode("ascii")
        connection = self._connection()
        try:
            connection.request(
                "POST",
                "/result",
                body=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            payload = response.read(HTTP_RESPONSE_LIMIT)
            return self._decode_json(response.status, payload, "GPT 写入")
        except (OSError, http.client.HTTPException) as exc:
            raise PartitionResizeError(
                "已提交 GPT 写入请求但未取得最终响应，状态未知。切勿断电或重复提交；"
                "请保持供电至少 10 分钟，再人工检查 U-Boot Web。"
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _require_success(payload: Mapping[str, Any], action: str) -> Mapping[str, Any]:
        if payload.get("status") != "success":
            raise PartitionResizeError(
                f"U-Boot {action}拒绝：{json.dumps(payload, ensure_ascii=False)}"
            )
        info = payload.get("info")
        if not isinstance(info, dict):
            raise PartitionResizeError(f"U-Boot {action}成功响应缺少 info。")
        return info

    def commit(
        self,
        backup: FullBackupInfo,
        generated: GeneratedGptInfo,
        confirm: ResizeConfirmationCallback,
    ) -> Path:
        """Upload, remotely verify, confirm, and write a prepared GPT exactly once."""

        version = self.probe_version(required=True)
        assert version is not None
        LOGGER.info("上传设备专属 GPT 到 U-Boot 内存（此阶段不写闪存）")
        upload_payload = self._post_file(Path(generated.path))
        upload_info = self._require_success(upload_payload, "GPT 上传校验")
        try:
            remote_size = int(str(upload_info.get("size", "")))
        except ValueError as exc:
            raise PartitionResizeError("U-Boot 返回的 GPT 大小无效。") from exc
        remote_md5 = str(upload_info.get("md5", "")).lower()
        remote_type = str(upload_info.get("type", ""))
        if remote_size != generated.size_bytes or remote_md5 != generated.md5:
            raise PartitionResizeError("U-Boot 内存中的 GPT 大小或 MD5 与本地不一致，禁止写入。")
        if remote_type != "GPT (Single Image for eMMC device)":
            raise PartitionResizeError(
                f"U-Boot 将文件识别为 {remote_type!r}，不是受支持的 eMMC GPT 镜像。"
            )
        phrase = "WRITE-GPT-" + generated.sha256[:12].upper()
        plan = RootfsResizePlan(
            generated_gpt=generated,
            backup=backup,
            web_url=self.web_url,
            uboot_version=version,
            upload_info=dict(upload_info),
            confirmation_phrase=phrase,
        )
        self.report.update(
            {
                "status": "remote-validation-complete",
                "uboot_version": version,
                "upload_response": upload_payload,
                "confirmation_phrase": phrase,
            }
        )
        self._write_report()
        if not confirm(plan):
            self.report["status"] = "cancelled-before-write"
            self._write_report()
            raise PartitionResizeError("用户未确认；GPT 只在 U-Boot 内存中，未写入 eMMC。")
        self.report["status"] = "write-request-submitted"
        self._write_report()
        LOGGER.warning("正在写入主/备 GPT；现在绝对不要断电、刷新页面或重复提交")
        try:
            result_payload = self._post_result()
        except PartitionResizeError as exc:
            self.report.update({"status": "write-result-unknown", "error": str(exc)})
            self._write_report()
            raise
        self._require_success(result_payload, "GPT 写入")
        self.report.update({"status": "complete", "result_response": result_payload})
        self._write_report()
        return self.report_path

    def resize(self, confirm: ResizeConfirmationCallback) -> Path:
        """Convenience wrapper for callers that do not need staged preparation."""

        backup, generated = self.prepare()
        return self.commit(backup, generated, confirm)
