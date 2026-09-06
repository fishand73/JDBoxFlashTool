"""Guarded U-Boot upload and dual-APPSBL flashing workflow."""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import socket
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Set, Tuple, Type
from urllib.parse import urlsplit

from .constants import (
    DEFAULT_UBOOT_IMAGE_MD5,
    DEFAULT_UBOOT_IMAGE_SHA256,
    DEFAULT_UBOOT_IMAGE_SIZE,
    UBOOT_ABORT_REPLY,
    UBOOT_ABORT_REQUEST,
)
from .errors import FlashError
from .integrity import hash_file
from .models import Partition
from .telnet_client import MiniTelnet
from .util import discover_local_ip, shell_quote

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class UbootImageInfo:
    """Locally validated fixed U-Boot image metadata."""

    path: str
    filename: str
    size_bytes: int
    md5: str
    sha256: str
    elf_machine: str
    network_abort_protocol: bool


@dataclass(frozen=True)
class UbootFlashPlan:
    """Everything shown to the user immediately before destructive writes."""

    image: UbootImageInfo
    model: str
    compatible: str
    targets: Tuple[str, str]
    write_order: Tuple[str, str]
    before_hashes: Mapping[str, Mapping[str, str]]
    backup_files: Mapping[str, str]
    confirmation_phrase: str


ConfirmationCallback = Callable[[UbootFlashPlan], bool]


def _router_addresses(host: str) -> Set[str]:
    addresses: Set[str] = {host}
    try:
        for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
            addresses.add(str(item[4][0]))
    except OSError:
        pass
    return addresses


def _make_file_handler(
    image_path: Path,
    route: str,
    allowed_peers: Set[str],
) -> Type[BaseHTTPRequestHandler]:
    class ExactFileHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            peer = str(self.client_address[0])
            if peer not in allowed_peers or urlsplit(self.path).path != route:
                self.send_error(404)
                return
            try:
                size = image_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with image_path.open("rb") as handle:
                    shutil.copyfileobj(handle, self.wfile, length=64 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ExactFileHandler


class UbootFlasher:
    """Flash the fixed image to APPSBL_1 first, then APPSBL, with readback checks."""

    TARGETS: Tuple[str, str] = ("/dev/mmcblk0p13", "/dev/mmcblk0p14")
    WRITE_ORDER: Tuple[str, str] = ("/dev/mmcblk0p14", "/dev/mmcblk0p13")

    def __init__(
        self,
        shell: MiniTelnet,
        router_host: str,
        output: Path,
        image_path: Path,
        *,
        pc_host: Optional[str] = None,
        listen_host: str = "0.0.0.0",
        listen_port: int = 0,
        command_timeout: float = 7200.0,
    ) -> None:
        self.shell = shell
        self.router_host = router_host
        self.output = output
        self.image_path = image_path
        self.pc_host = pc_host
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.command_timeout = command_timeout
        self.remote_path = f"/tmp/.athena_uboot_{secrets.token_hex(6)}.bin"
        self.report_path = output / "uboot-flash-report.json"

    def validate_image(self) -> UbootImageInfo:
        """Require the exact requested build, size, and ARM ELF header."""

        path = self.image_path.expanduser().resolve()
        if not path.is_file():
            raise FlashError(f"找不到 U-Boot 镜像: {path}")
        size = path.stat().st_size
        if size != DEFAULT_UBOOT_IMAGE_SIZE:
            raise FlashError(
                f"U-Boot 镜像大小不符: {size} != {DEFAULT_UBOOT_IMAGE_SIZE} bytes"
            )
        md5, sha256 = hash_file(path)
        if md5 != DEFAULT_UBOOT_IMAGE_MD5 or sha256 != DEFAULT_UBOOT_IMAGE_SHA256:
            raise FlashError(
                "U-Boot 镜像不是项目锁定的 260816_142236_3011049 版本：\n"
                f"  MD5: {md5}\n  SHA256: {sha256}"
            )
        content = path.read_bytes()
        header = content[:20]
        if len(header) < 20 or header[:4] != b"\x7fELF":
            raise FlashError("U-Boot 镜像不是 ELF 文件。")
        if header[4] != 1 or header[5] != 1:
            raise FlashError("U-Boot ELF 不是 32-bit little-endian 格式。")
        machine = int.from_bytes(header[18:20], byteorder="little")
        if machine != 40:
            raise FlashError(f"U-Boot ELF 架构不是 ARM (e_machine={machine})。")
        if UBOOT_ABORT_REQUEST not in content or UBOOT_ABORT_REPLY not in content:
            raise FlashError("U-Boot 镜像不包含 uBootEnter 的请求/回复协议标记。")
        return UbootImageInfo(
            path=str(path),
            filename=path.name,
            size_bytes=size,
            md5=md5,
            sha256=sha256,
            elf_machine="ARM (EM_ARM=40)",
            network_abort_protocol=True,
        )

    def validate_device(
        self,
        partitions: Mapping[int, Partition],
        model: str,
        release: str,
    ) -> str:
        """Require exact labels/sizes plus an IPQ6018 JDCloud device identity."""

        compatible = self.shell.run(
            "cat /sys/firmware/devicetree/base/compatible 2>/dev/null "
            "| tr '\\000' '\\n' || true",
            check=False,
        ).strip()
        identity = "\n".join((model, release, compatible)).upper()
        if "IPQ6018" not in identity or "JDCLOUD" not in identity:
            raise FlashError(
                "设备身份不是已知的 JDCloud IPQ6018 雅典娜，禁止刷写 U-Boot：\n"
                f"model={model!r}\ncompatible={compatible!r}"
            )
        expected = {13: "0:APPSBL", 14: "0:APPSBL_1"}
        for number, label in expected.items():
            partition = partitions.get(number)
            if partition is None:
                raise FlashError(f"缺少 U-Boot 目标分区 p{number}。")
            if partition.label != label:
                raise FlashError(
                    f"p{number} 标签不符: {partition.label!r} != {label!r}；禁止强制绕过。"
                )
            if partition.size_bytes != DEFAULT_UBOOT_IMAGE_SIZE:
                raise FlashError(
                    f"p{number} 大小不符: {partition.size_bytes} != "
                    f"{DEFAULT_UBOOT_IMAGE_SIZE} bytes"
                )
        mounted = self.shell.run(
            "grep -E 'mmcblk0p(13|14)( |$)' /proc/mounts 2>/dev/null || true",
            check=False,
        ).strip()
        if mounted:
            raise FlashError(f"APPSBL 分区意外处于挂载状态，禁止刷写:\n{mounted}")
        return compatible

    def _remote_hashes(self, device_or_file: str) -> Dict[str, str]:
        output = self.shell.run(
            f"wc -c < {shell_quote(device_or_file)}; "
            f"busybox md5sum {shell_quote(device_or_file)}; "
            f"busybox sha256sum {shell_quote(device_or_file)}",
            timeout=self.command_timeout,
        )
        size = re.search(r"(?m)^\s*(\d+)\s*$", output)
        md5 = re.search(r"(?mi)^([0-9a-f]{32})\s+", output)
        sha256 = re.search(r"(?mi)^([0-9a-f]{64})\s+", output)
        if not size or not md5 or not sha256:
            raise FlashError(f"无法解析远端校验信息: {output}")
        return {
            "size_bytes": size.group(1),
            "md5": md5.group(1).lower(),
            "sha256": sha256.group(1).lower(),
        }

    def _upload(self, image: UbootImageInfo) -> None:
        applets = set(self.shell.run("busybox --list 2>/dev/null", check=False).splitlines())
        missing = {"dd", "md5sum", "sha256sum", "wget"}.difference(applets)
        if missing:
            raise FlashError("BusyBox 缺少 U-Boot 刷写所需 applet: " + ", ".join(sorted(missing)))
        local_address = self.pc_host or discover_local_ip(self.router_host)
        token = secrets.token_hex(12)
        route = f"/{token}/{image.filename}"
        handler = _make_file_handler(Path(image.path), route, _router_addresses(self.router_host))
        try:
            server = ThreadingHTTPServer((self.listen_host, self.listen_port), handler)
        except OSError as exc:
            raise FlashError(f"无法打开本机 U-Boot 上传端口: {exc}") from exc
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, name="uboot-upload", daemon=True)
        thread.start()
        url = f"http://{local_address}:{port}{route}"
        LOGGER.info("将 U-Boot 镜像传到路由器临时内存文件（不会写分区）")
        try:
            self.shell.run(
                f"rm -f {shell_quote(self.remote_path)}; "
                f"busybox wget -q -O {shell_quote(self.remote_path)} {shell_quote(url)}",
                timeout=120.0,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)
        remote = self._remote_hashes(self.remote_path)
        if (
            int(remote["size_bytes"]) != image.size_bytes
            or remote["md5"] != image.md5
            or remote["sha256"] != image.sha256
        ):
            raise FlashError(f"U-Boot 上传后校验失败: {remote}")

    def _write_target(self, target: str, image: UbootImageInfo) -> Dict[str, str]:
        LOGGER.warning("正在写入 %s；此时绝对不要断电", target)
        self.shell.run(
            f"dd if={shell_quote(self.remote_path)} of={shell_quote(target)} "
            "bs=64K conv=fsync && sync",
            timeout=self.command_timeout,
        )
        hashes = self._remote_hashes(target)
        if (
            int(hashes["size_bytes"]) != image.size_bytes
            or hashes["md5"] != image.md5
            or hashes["sha256"] != image.sha256
        ):
            raise FlashError(f"{target} 写入后的回读校验失败: {hashes}")
        return hashes

    def _write_report(self, payload: Mapping[str, Any]) -> None:
        self.report_path.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def flash(
        self,
        partitions: Mapping[int, Partition],
        model: str,
        release: str,
        backup_files: Mapping[str, str],
        confirm: ConfirmationCallback,
    ) -> Path:
        """Run all preflights, require confirmation, then flash and verify both copies."""

        image = self.validate_image()
        compatible = self.validate_device(partitions, model, release)
        before_hashes = {target: self._remote_hashes(target) for target in self.TARGETS}
        phrase = "FLASH-UBOOT-" + image.sha256[:12].upper()
        plan = UbootFlashPlan(
            image=image,
            model=model,
            compatible=compatible,
            targets=self.TARGETS,
            write_order=self.WRITE_ORDER,
            before_hashes=before_hashes,
            backup_files=dict(backup_files),
            confirmation_phrase=phrase,
        )
        report: Dict[str, Any] = {
            "schema_version": 1,
            "status": "preflight",
            "automatic_reboot": False,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "plan": asdict(plan),
            "written_targets": {},
        }
        self._write_report(report)
        try:
            self._upload(image)
            if not confirm(plan):
                report["status"] = "cancelled"
                report["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._write_report(report)
                raise FlashError("用户未确认，未写入任何 U-Boot 分区。")
            report["status"] = "writing"
            report["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write_report(report)
            written = report["written_targets"]
            if not isinstance(written, dict):  # pragma: no cover - internal invariant
                raise FlashError("内部刷写报告状态异常。")
            for target in self.WRITE_ORDER:
                written[target] = self._write_target(target, image)
                report["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._write_report(report)
            report["status"] = "complete"
            report["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._write_report(report)
            return self.report_path
        except Exception as exc:
            if report.get("status") not in {"cancelled", "complete"}:
                report["status"] = "failed"
                report["error"] = str(exc)
                report["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._write_report(report)
            raise
        finally:
            self.shell.run(f"rm -f {shell_quote(self.remote_path)}", check=False)
