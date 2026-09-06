"""Small data models used across modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Partition:
    """One eMMC partition reported by ``/proc/partitions``."""

    number: int
    name: str
    size_bytes: int
    label: Optional[str] = None


@dataclass(frozen=True)
class Artifact:
    """One locally verified backup artifact."""

    filename: str
    source: str
    size_bytes: int
    md5: str
    sha256: str
    kind: str
    partition_number: Optional[int] = None
    partition_label: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""

        return asdict(self)

