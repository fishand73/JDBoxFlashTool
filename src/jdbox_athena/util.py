"""General helpers with no router-side effects."""

from __future__ import annotations

import re
import shlex
import socket
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

from .errors import AthenaError


def shell_quote(value: str) -> str:
    """Quote one value for the router's BusyBox shell."""

    return shlex.quote(value)


def human_size(value: int) -> str:
    """Format a byte count for console output."""

    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def safe_filename(value: str) -> str:
    """Turn a partition label into a portable filename component."""

    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_.")
    return cleaned or "unlabeled"


def normalize_management_url(value: str) -> Tuple[str, str]:
    """Normalize an HTTP management URL and return ``(url, hostname)``."""

    candidate = value.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise AthenaError("管理地址仅支持 http:// 或 https://。")
    if not parsed.hostname:
        raise AthenaError(f"无效的管理地址: {value}")
    path = parsed.path.rstrip("/") + "/"
    normalized = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    return normalized, parsed.hostname


def is_tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """Return whether a TCP connection can be established."""

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_tcp(host: str, port: int, timeout: float, interval: float = 1.0) -> bool:
    """Wait until a TCP endpoint becomes reachable."""

    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_tcp_open(host, port):
            return True
        time.sleep(interval)
    return is_tcp_open(host, port)


def discover_local_ip(router_host: str) -> str:
    """Find the local address selected by the OS for the route to the router."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((router_host, 9))
        address = str(probe.getsockname()[0])
    except OSError as exc:
        raise AthenaError("无法自动确定电脑的局域网 IP，请使用 --pc-host 指定。") from exc
    finally:
        probe.close()
    if not address or address.startswith("127."):
        raise AthenaError("自动检测到的电脑 IP 不可用，请使用 --pc-host 指定局域网 IP。")
    return address


def resolved_output(path: Optional[str], default_name: str) -> Path:
    """Resolve and create the local output directory."""

    output = Path(path).expanduser().resolve() if path else Path(default_name).resolve()
    output.mkdir(parents=True, exist_ok=True)
    return output

