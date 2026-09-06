from __future__ import annotations

import unittest
from typing import Dict, Tuple

from jdbox_athena.device import DeviceInspector
from jdbox_athena.errors import DeviceMismatchError


class FakeShell:
    def __init__(self, responses: Dict[str, str]) -> None:
        self.responses = responses

    def run(self, command: str, **_kwargs: object) -> str:
        for needle, response in self.responses.items():
            if needle in command:
                return response
        return ""

    def exec(self, _command: str, **_kwargs: object) -> Tuple[str, int]:
        return "", 0


class DeviceTests(unittest.TestCase):
    def build_inspector(self) -> DeviceInspector:
        table = "\n".join(
            f" 179 {number} 1024 mmcblk0p{number}" for number in range(1, 27)
        )
        blkid = "\n".join(
            (
                '/dev/mmcblk0p13: PARTLABEL="0:APPSBL"',
                '/dev/mmcblk0p15: PARTLABEL="0:ART"',
            )
        )
        shell = FakeShell({"blkid": blkid, "/proc/partitions": table})
        return DeviceInspector(shell)  # type: ignore[arg-type]

    def test_parses_and_accepts_expected_layout(self) -> None:
        inspector = self.build_inspector()
        partitions = inspector.read_partitions()
        self.assertEqual(len(partitions), 26)
        self.assertEqual(partitions[15].label, "0:ART")
        inspector.verify(partitions)

    def test_missing_partition_cannot_be_forced(self) -> None:
        inspector = self.build_inspector()
        partitions = inspector.read_partitions()
        del partitions[26]
        with self.assertRaises(DeviceMismatchError):
            inspector.verify(partitions, force=True)


if __name__ == "__main__":
    unittest.main()
