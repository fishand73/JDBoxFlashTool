"""Enter compatible U-Boot Web recovery by sending its network abort packet.

Protocol and interface filtering are adapted from chenxin527/uBootEnter,
commit 1c4185c49653465bbfa437c7fdf45c5eabcc7039, under the MIT License.

Copyright (c) 2026 chenxin527
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import importlib
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence

from .constants import (
    UBOOT_ABORT_REPLY,
    UBOOT_ABORT_REPLY_PORT,
    UBOOT_ABORT_REQUEST,
    UBOOT_ABORT_TARGET_PORT,
)
from .errors import OperationCancelled, UbootEnterError

LOGGER = logging.getLogger(__name__)
TARGET_MAC = "ff:ff:ff:ff:ff:ff"
TARGET_IP = "255.255.255.255"
SOURCE_IP = "0.0.0.0"
SOURCE_PORT = 40508
VIRTUAL_INTERFACE_KEYWORDS = (
    "virtual",
    "vpn",
    "tunnel",
    "loopback",
    "环回",
    "hyper-v",
    "virtualbox",
    "vmware",
    "wsl",
    "bluetooth",
    "wi-fi direct",
    "microsoft wi-fi direct",
    "teredo",
    "isatap",
    "6to4",
    "miniport",
    "wan miniport",
    "pseudo",
    "ndis",
    "vethernet",
    "usb over ethernet",
)


@dataclass(frozen=True)
class InterfaceInfo:
    """A Scapy interface plus stable display fields."""

    index: int
    name: str
    description: str
    mac: str
    handle: Any


@dataclass(frozen=True)
class UbootEnterResult:
    """Successful abort and Web readiness result."""

    reply_ip: str
    version: str
    attempts: int
    elapsed_seconds: float
    web_url: str


def is_physical_interface(interface: Any) -> bool:
    """Filter the same common virtual/tunnel adapters as upstream."""

    description = str(getattr(interface, "description", "") or "")
    name = str(getattr(interface, "name", "") or "")
    if not description:
        return False
    lowered = f"{name}\n{description}".lower()
    if any(keyword in lowered for keyword in VIRTUAL_INTERFACE_KEYWORDS):
        return False
    mac = str(getattr(interface, "mac", "") or "")
    compact_mac = mac.replace(":", "").replace("-", "").upper()
    return compact_mac not in {"", "000000000000"}


def _load_scapy() -> Any:
    try:
        return importlib.import_module("scapy.all")
    except ImportError as exc:
        raise UbootEnterError(
            "uBootEnter 需要可选依赖 Scapy。请运行 "
            "python -m pip install -e \".[uboot-enter]\"；Windows 还必须安装 Npcap。"
        ) from exc


def discover_interfaces(scapy: Optional[Any] = None) -> List[InterfaceInfo]:
    """Return physical interfaces using upstream-compatible global indices."""

    module = scapy or _load_scapy()
    values = list(module.IFACES.data.values())
    result: List[InterfaceInfo] = []
    for index, interface in enumerate(values):
        if not is_physical_interface(interface):
            continue
        result.append(
            InterfaceInfo(
                index=index,
                name=str(getattr(interface, "name", "") or ""),
                description=str(getattr(interface, "description", "") or ""),
                mac=str(getattr(interface, "mac", "") or ""),
                handle=interface,
            )
        )
    return result


def resolve_interfaces(
    interfaces: Sequence[InterfaceInfo],
    selection: Optional[str],
) -> List[InterfaceInfo]:
    """Resolve ``all``, an upstream numeric index, or a name substring."""

    if not interfaces:
        raise UbootEnterError("没有找到物理网卡；请确认网卡已启用且 Npcap 可用。")
    value = (selection or "all").strip()
    if value.lower() in {"", "all", "auto"}:
        return list(interfaces)
    try:
        index = int(value)
    except ValueError:
        index = -1
    if index >= 0:
        matches = [interface for interface in interfaces if interface.index == index]
        if not matches:
            available = ", ".join(str(interface.index) for interface in interfaces)
            raise UbootEnterError(f"网卡索引 {index} 不存在；可用索引: {available}")
        return matches
    lowered = value.lower()
    matches = [
        interface
        for interface in interfaces
        if lowered in interface.name.lower() or lowered in interface.description.lower()
    ]
    if not matches:
        raise UbootEnterError(f"没有找到名称包含 {value!r} 的物理网卡。")
    if len(matches) > 1:
        descriptions = ", ".join(f"[{item.index}] {item.description}" for item in matches)
        raise UbootEnterError(f"网卡名称匹配不唯一，请改用索引: {descriptions}")
    return matches


class _ReplyListener:
    """Listen through both Scapy/Npcap and an ordinary UDP socket."""

    def __init__(self, scapy: Any, interfaces: Sequence[InterfaceInfo]) -> None:
        self.scapy = scapy
        self.interfaces = interfaces
        self.event = threading.Event()
        self.stop_event = threading.Event()
        self.reply_ip: Optional[str] = None
        self.sniffers: List[Any] = []
        self.udp_socket: Optional[socket.socket] = None
        self.udp_thread: Optional[threading.Thread] = None

    def _accept(self, payload: bytes, source_ip: Optional[str]) -> None:
        if payload == UBOOT_ABORT_REPLY and source_ip:
            self.reply_ip = source_ip
            self.event.set()

    def _process_packet(self, packet: Any) -> None:
        try:
            if self.scapy.Raw not in packet:
                return
            payload = bytes(packet[self.scapy.Raw].load)
            source_ip = str(packet[self.scapy.IP].src) if self.scapy.IP in packet else None
            self._accept(payload, source_ip)
        except Exception:
            LOGGER.debug("忽略无法解析的 U-Boot 回复包", exc_info=True)

    def _udp_loop(self) -> None:
        if self.udp_socket is None:
            return
        while not self.stop_event.is_set() and not self.event.is_set():
            try:
                payload, address = self.udp_socket.recvfrom(2048)
                self._accept(payload, str(address[0]))
            except socket.timeout:
                continue
            except OSError:
                return

    def start(self) -> None:
        for interface in self.interfaces:
            try:
                sniffer = self.scapy.AsyncSniffer(
                    iface=interface.handle,
                    filter=f"udp and dst port {UBOOT_ABORT_REPLY_PORT}",
                    prn=self._process_packet,
                    store=False,
                )
                sniffer.start()
                self.sniffers.append(sniffer)
            except Exception:
                LOGGER.debug("无法在 %s 启动 Npcap 监听", interface.description, exc_info=True)
        try:
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp_socket.bind(("0.0.0.0", UBOOT_ABORT_REPLY_PORT))
            udp_socket.settimeout(0.2)
            self.udp_socket = udp_socket
            self.udp_thread = threading.Thread(
                target=self._udp_loop,
                name="uboot-enter-udp-listener",
                daemon=True,
            )
            self.udp_thread.start()
        except OSError:
            LOGGER.debug("无法绑定 UDP 回复端口 %d", UBOOT_ABORT_REPLY_PORT, exc_info=True)
        if not self.sniffers and self.udp_socket is None:
            raise UbootEnterError("无法启动数据包监听；Windows 请安装并启用 Npcap。")

    def stop(self) -> None:
        self.stop_event.set()
        if self.udp_socket:
            self.udp_socket.close()
        if self.udp_thread:
            self.udp_thread.join(timeout=1.0)
        for sniffer in self.sniffers:
            with suppress(Exception):
                sniffer.stop()


class UbootEnterService:
    """Send the compatible abort packet until U-Boot confirms and serves HTTP."""

    def __init__(self, scapy: Optional[Any] = None) -> None:
        self.scapy = scapy or _load_scapy()

    def list_interfaces(self) -> List[InterfaceInfo]:
        return discover_interfaces(self.scapy)

    def _send(self, interface: InterfaceInfo) -> bool:
        try:
            source_mac = interface.mac or "02:00:00:00:00:01"
            packet = (
                self.scapy.Ether(dst=TARGET_MAC, src=source_mac)
                / self.scapy.IP(src=SOURCE_IP, dst=TARGET_IP, ttl=64, flags="DF")
                / self.scapy.UDP(sport=SOURCE_PORT, dport=UBOOT_ABORT_TARGET_PORT)
                / self.scapy.Raw(load=UBOOT_ABORT_REQUEST)
            )
            self.scapy.sendp(packet, iface=interface.handle, verbose=False)
            return True
        except Exception:
            LOGGER.debug("%s 发送中断包失败", interface.description, exc_info=True)
            return False

    @staticmethod
    def _check_http(ip_address: str, timeout: float = 2.0) -> Optional[str]:
        request = urllib.request.Request(
            f"http://{ip_address}/version",
            headers={"User-Agent": "JDBox-Athena-uBootEnter/0.3"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    return None
                version = response.read(1024).decode("utf-8", errors="replace").strip()
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        return version if version.startswith("U-Boot") else None

    @staticmethod
    def _raise_if_cancelled(cancel_event: Optional[threading.Event]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled(
                "已取消等待 U-Boot 启动，未执行后续固件写入。"
                "如果已经收到 UBOOT:ABORTED，路由器可能仍停留在 U-Boot；"
                "请手动访问管理地址或重启路由器。"
            )

    def _wait_http(
        self,
        ip_address: str,
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_cancelled(cancel_event)
            version = self._check_http(ip_address)
            if version:
                return version
            if cancel_event is not None:
                if cancel_event.wait(0.5):
                    self._raise_if_cancelled(cancel_event)
            else:
                time.sleep(0.5)
        return None

    def run(
        self,
        selection: Optional[str] = "all",
        *,
        timeout: float = 120.0,
        interval: float = 0.3,
        http_timeout: float = 15.0,
        open_browser: bool = True,
        cancel_event: Optional[threading.Event] = None,
    ) -> UbootEnterResult:
        """Run until success or timeout; the user powers/reboots the router separately."""

        self._raise_if_cancelled(cancel_event)
        interfaces = resolve_interfaces(self.list_interfaces(), selection)
        self._raise_if_cancelled(cancel_event)
        LOGGER.info(
            "uBootEnter 网卡: %s",
            ", ".join(f"[{item.index}] {item.description}" for item in interfaces),
        )
        LOGGER.info(
            "开始发送 %s；现在请给路由器通电或重启",
            UBOOT_ABORT_REQUEST.decode("ascii"),
        )
        listener = _ReplyListener(self.scapy, interfaces)
        listener.start()
        started = time.monotonic()
        attempts = 0
        consecutive_send_failures = 0
        try:
            while time.monotonic() - started < timeout:
                self._raise_if_cancelled(cancel_event)
                attempts += 1
                sent = [self._send(interface) for interface in interfaces]
                if any(sent):
                    consecutive_send_failures = 0
                else:
                    consecutive_send_failures += 1
                if consecutive_send_failures >= 5:
                    raise UbootEnterError(
                        "连续五轮无法发送二层数据包；Windows 请确认 Npcap 已安装，"
                        "并以管理员权限运行终端。"
                    )
                if listener.event.wait(interval):
                    break
                self._raise_if_cancelled(cancel_event)
                if attempts % 10 == 0:
                    LOGGER.info("已发送 %d 轮中断包，继续等待 U-Boot 启动", attempts)
        finally:
            listener.stop()
        elapsed = time.monotonic() - started
        if not listener.event.is_set() or not listener.reply_ip:
            raise UbootEnterError(
                f"{timeout:.0f} 秒内未收到 {UBOOT_ABORT_REPLY.decode('ascii')}；"
                "请确认网线直连 LAN、使用配套 U-Boot，并在工具运行后重启路由器。"
            )
        reply_ip = listener.reply_ip
        LOGGER.info("收到 %s，U-Boot IP: %s", UBOOT_ABORT_REPLY.decode("ascii"), reply_ip)
        self._raise_if_cancelled(cancel_event)
        version = self._wait_http(reply_ip, http_timeout, cancel_event)
        if not version:
            raise UbootEnterError(
                f"已中断自动启动，但 U-Boot HTTP 未在 {http_timeout:.0f} 秒内就绪；"
                f"请手动访问 http://{reply_ip}/。"
            )
        web_url = f"http://{reply_ip}/"
        LOGGER.info("U-Boot Web 已就绪: %s (%s)", web_url, version)
        if open_browser:
            try:
                webbrowser.open(web_url, new=2)
            except Exception:
                LOGGER.warning("无法自动打开浏览器，请手动访问 %s", web_url)
        return UbootEnterResult(
            reply_ip=reply_ip,
            version=version,
            attempts=attempts,
            elapsed_seconds=elapsed,
            web_url=web_url,
        )


def format_interfaces(interfaces: Iterable[InterfaceInfo]) -> str:
    """Create a stable plain-text interface table for the CLI."""

    rows = ["索引  MAC 地址             接口"]
    for item in interfaces:
        rows.append(f"[{item.index:<3}] {item.mac:<20} {item.description or item.name}")
    return os.linesep.join(rows)
