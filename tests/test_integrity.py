from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jdbox_athena.integrity import hash_file, write_manifests
from jdbox_athena.models import Artifact


class IntegrityTests(unittest.TestCase):
    def test_hash_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"athena-backup-smoke-test"
            image = root / "p15_ART.bin"
            image.write_bytes(content)
            md5, sha256 = hash_file(image)
            self.assertEqual(md5, hashlib.md5(content).hexdigest())
            self.assertEqual(sha256, hashlib.sha256(content).hexdigest())
            artifact = Artifact(
                filename=image.name,
                source="/dev/mmcblk0p15",
                size_bytes=len(content),
                md5=md5,
                sha256=sha256,
                kind="partition",
                partition_number=15,
                partition_label="0:ART",
            )
            write_manifests(root, [artifact])
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["read_only_backup"])
            self.assertEqual(manifest["artifacts"][0]["sha256"], sha256)
            self.assertIn(image.name, (root / "MD5SUMS").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
