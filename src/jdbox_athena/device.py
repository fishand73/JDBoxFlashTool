"""Athena AX6600 discovery and source-layout validation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Mapping, Tuple

from .constants import FIRST_PARTITION, LAST_PARTITION
from .errors import DeviceMismatchError
from .models import Partition
from .telnet_client import MiniTelnet

INFO_COMMANDS: Mapping[str, str] = {
    "date": "date 2>/dev/null || true",
    "uname": "uname -a 2>/dev/null || true",
    "hostname": "hostname 2>/dev/null || true",
    "model": (
        "cat /sys/firmware/devicetree/base/model 2>/dev/null "
        "| tr '\\000' '\\n' || true"
    ),
    "cmdline": "cat /proc/cmdline 2>/dev/null || true",
    "openwrt_release": "cat /etc/openwrt_release 2>/dev/null || true",
    "os_release": "cat /etc/os-release 2>/dev/null || true",
    "blkid": "blkid 2>/dev/null || true",
    "partitions": "cat /proc/partitions",
    "mounts": "cat /proc/mounts",
    "df": "df -h",
    "mmc_sectors": "cat /sys/class/block/mmcblk0/size 2>/dev/null || true",
    "logical_block_size": (
        "cat /sys/class/block/mmcblk0/queue/logical_block_size 2>/dev/null || true"
    ),
}


class DeviceInspector:
    """Read device metadata and reject surprising block layouts by default."""

    def __init__(self, shell: MiniTelnet) -> None:
        self.shell = shell

    def collect(self, destination: Path) -> Dict[str, str]:
        """Collect diagnostic metadata without changing the router."""

        values: Dict[str, str] = {}
        chunks = []
        for name, command in INFO_COMMANDS.items():
            try:
                value = self.shell.run(command, check=False)
            except Exception as exc:  # diagnostics should not hide the useful fields
                value = f"ERROR: {exc}"
            values[name] = value
            chunks.append(f"===== {name} =====\n{value}\n")
        destination.write_text("\n".join(chunks), encoding="utf-8")
        return values

    def read_labels(self) -> Dict[int, str]:
        """Read partition labels from blkid, then supplement them from sysfs."""

        labels: Dict[int, str] = {}
        blkid = self.shell.run("blkid 2>/dev/null || true", check=False)
        for line in blkid.splitlines():
            device = re.search(r"/dev/mmcblk0p(\d+):", line)
            label = re.search(r'PARTLABEL="([^"]+)"', line)
            if device and label:
                labels[int(device.group(1))] = label.group(1)
        sysfs = self.shell.run(
            "for f in /sys/class/block/mmcblk0p*/uevent; do "
            "printf '%s|' \"$f\"; grep '^PARTNAME=' \"$f\" 2>/dev/null; done",
            check=False,
        )
        for line in sysfs.splitlines():
            match = re.search(r"mmcblk0p(\d+)/uevent\|PARTNAME=(.*)$", line)
            if match and match.group(2):
                labels.setdefault(int(match.group(1)), match.group(2).strip())
        return labels

    def read_partitions(self) -> Dict[int, Partition]:
        """Parse p1 through p26 from ``/proc/partitions``."""

        labels = self.read_labels()
        table = self.shell.run("cat /proc/partitions")
        partitions: Dict[int, Partition] = {}
        pattern = re.compile(r"^\s*\d+\s+\d+\s+(\d+)\s+mmcblk0p(\d+)\s*$")
        for line in table.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            number = int(match.group(2))
            partitions[number] = Partition(
                number=number,
                name=f"mmcblk0p{number}",
                size_bytes=int(match.group(1)) * 1024,
                label=labels.get(number),
            )
        return partitions

    def verify(self, partitions: Mapping[int, Partition], force: bool = False) -> None:
        """Require all p1-p26 and characteristic APPSBL/ART labels."""

        expected = set(range(FIRST_PARTITION, LAST_PARTITION + 1))
        missing = sorted(expected.difference(partitions))
        if missing:
            raise DeviceMismatchError(
                "缺少必须备份的分区: " + ", ".join(f"p{number}" for number in missing)
            )
        problems = []
        p13 = partitions.get(13)
        p15 = partitions.get(15)
        if p13 is None or not p13.label or "APPSBL" not in p13.label.upper():
            problems.append("p13 的 PARTLABEL 不是 APPSBL")
        if p15 is None or not p15.label or "ART" not in p15.label.upper():
            problems.append("p15 的 PARTLABEL 不是 ART")
        if problems and not force:
            raise DeviceMismatchError(
                "检测到的布局不像雅典娜 AX6600 原厂 eMMC；为避免备份错设备已停止：\n  - "
                + "\n  - ".join(problems)
                + "\n确认无误后才可使用 --force-device。"
            )

    def disk_geometry(self) -> Tuple[int, int]:
        """Return ``(total logical sectors, logical sector bytes)``."""

        sectors_text = self.shell.run("cat /sys/class/block/mmcblk0/size").strip()
        sector_size_text = self.shell.run(
            "cat /sys/class/block/mmcblk0/queue/logical_block_size"
        ).strip()
        try:
            kernel_sectors = int(sectors_text.splitlines()[-1])
            sector_size = int(sector_size_text.splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise DeviceMismatchError("无法读取 mmcblk0 的扇区几何信息。") from exc
        total_bytes = kernel_sectors * 512
        if (
            kernel_sectors < 67
            or sector_size not in {512, 1024, 2048, 4096}
            or total_bytes % sector_size != 0
        ):
            raise DeviceMismatchError(
                "mmcblk0 几何信息异常: "
                f"kernel_sectors={kernel_sectors}, sector_size={sector_size}"
            )
        sectors = total_bytes // sector_size
        return sectors, sector_size
