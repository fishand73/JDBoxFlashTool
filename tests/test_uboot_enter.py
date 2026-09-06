from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from jdbox_athena.constants import (
    UBOOT_ABORT_REPLY,
    UBOOT_ABORT_REPLY_PORT,
    UBOOT_ABORT_REQUEST,
    UBOOT_ABORT_TARGET_PORT,
)
from jdbox_athena.errors import OperationCancelled, UbootEnterError
from jdbox_athena.uboot_enter import (
    InterfaceInfo,
    UbootEnterService,
    is_physical_interface,
    resolve_interfaces,
)


class DummyInterface:
    def __init__(self, name: str, description: str, mac: str) -> None:
        self.name = name
        self.description = description
        self.mac = mac


class DummyReplyListener:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.reply_ip = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class UbootEnterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.interfaces = [
            InterfaceInfo(2, "ethernet-a", "Realtek PCIe Ethernet", "00:11:22:33:44:55", object()),
            InterfaceInfo(7, "ethernet-b", "USB Ethernet", "00:11:22:33:44:66", object()),
        ]

    def test_protocol_constants_match_upstream(self) -> None:
        self.assertEqual(UBOOT_ABORT_REQUEST, b"UBOOT:ABORT")
        self.assertEqual(UBOOT_ABORT_REPLY, b"UBOOT:ABORTED")
        self.assertEqual(UBOOT_ABORT_TARGET_PORT, 37541)
        self.assertEqual(UBOOT_ABORT_REPLY_PORT, 37540)

    def test_virtual_and_zero_mac_interfaces_are_filtered(self) -> None:
        self.assertFalse(
            is_physical_interface(
                DummyInterface("vEthernet", "Hyper-V Virtual Ethernet", "00:11:22:33:44:55")
            )
        )
        self.assertFalse(
            is_physical_interface(DummyInterface("eth0", "Ethernet", "00:00:00:00:00:00"))
        )
        self.assertTrue(
            is_physical_interface(
                DummyInterface("eth0", "Realtek PCIe Ethernet", "00:11:22:33:44:55")
            )
        )

    def test_interface_resolution_supports_all_index_and_name(self) -> None:
        self.assertEqual(len(resolve_interfaces(self.interfaces, "all")), 2)
        self.assertEqual(resolve_interfaces(self.interfaces, "7")[0].index, 7)
        self.assertEqual(resolve_interfaces(self.interfaces, "Realtek")[0].index, 2)

    def test_ambiguous_or_missing_selection_is_rejected(self) -> None:
        with self.assertRaises(UbootEnterError):
            resolve_interfaces(self.interfaces, "Ethernet")
        with self.assertRaises(UbootEnterError):
            resolve_interfaces(self.interfaces, "99")

    def test_pre_cancelled_wait_stops_before_listener_starts(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        service = UbootEnterService(scapy=object())
        with (
            patch.object(service, "list_interfaces", return_value=self.interfaces),
            self.assertRaises(OperationCancelled),
        ):
            service.run(cancel_event=cancel_event)

    def test_cancel_during_wait_stops_listener(self) -> None:
        cancel_event = threading.Event()
        listener = DummyReplyListener()
        service = UbootEnterService(scapy=object())

        def cancel_after_send(_interface: InterfaceInfo) -> bool:
            cancel_event.set()
            return True

        with (
            patch.object(service, "list_interfaces", return_value=self.interfaces),
            patch.object(service, "_send", side_effect=cancel_after_send),
            patch("jdbox_athena.uboot_enter._ReplyListener", return_value=listener),
            self.assertRaises(OperationCancelled),
        ):
            service.run(cancel_event=cancel_event, interval=0.01)
        self.assertTrue(listener.started)
        self.assertTrue(listener.stopped)


if __name__ == "__main__":
    unittest.main()
