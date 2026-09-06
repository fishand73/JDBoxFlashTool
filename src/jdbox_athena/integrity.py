"""Local checksum and manifest helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Tuple

from .models import Artifact

CHUNK_SIZE = 4 * 1024 * 1024


def hash_file(path: Path) -> Tuple[str, str]:
    """Calculate MD5 and SHA-256 in one pass."""

    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK_SIZE)
            if not block:
                break
            md5.update(block)
            sha256.update(block)
    return md5.hexdigest(), sha256.hexdigest()


def write_manifests(output: Path, artifacts: Iterable[Artifact]) -> None:
    """Write machine-readable and conventional checksum manifests."""

    ordered = list(artifacts)
    payload = {
        "schema_version": 1,
        "read_only_backup": True,
        "artifacts": [artifact.to_dict() for artifact in ordered],
    }
    (output / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "MD5SUMS").write_text(
        "".join(f"{artifact.md5}  {artifact.filename}\n" for artifact in ordered),
        encoding="utf-8",
    )
    (output / "SHA256SUMS").write_text(
        "".join(f"{artifact.sha256}  {artifact.filename}\n" for artifact in ordered),
        encoding="utf-8",
    )
