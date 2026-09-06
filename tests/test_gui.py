from __future__ import annotations

import logging
import tempfile
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path

from jdbox_athena.gui import (
    AUTO_PC_HOST,
    AUTO_REMOTE_TARGET,
    AthenaGui,
    ConfirmationRequest,
    TypedConfirmationDialog,
    application_root,
    default_output_parent,
    dpi_scale,
    interface_selection,
    new_output_path,
    optional_manual_value,
    scaled_window_size,
)


class GuiHelperTests(unittest.TestCase):
    def test_cancel_button_is_visible_and_sets_safe_cancel_event(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        app = None
        try:
            app = AthenaGui(root)
            self.assertTrue(app.cancel_button.pack_info())
            self.assertEqual(str(app.cancel_button["state"]), "disabled")
            app.running = True
            with app.cancel_lock:
                app.cancel_allowed = True
            app.cancel_button.configure(state="normal")
            app._cancel_current()
            self.assertTrue(app.cancel_event.is_set())
            self.assertEqual(str(app.cancel_button["state"]), "disabled")
            self.assertEqual(app.status.get(), "正在安全取消…")
            app.running = False
        finally:
            if app is not None:
                logging.getLogger().removeHandler(app.log_handler)
            root.destroy()

    def test_confirmation_button_uses_visible_button_bar(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        dialog = None
        try:
            request = ConfirmationRequest(
                title="测试确认",
                warning="测试警告",
                details="测试详情",
                phrase="FLASH-TEST-1234",
            )
            dialog = TypedConfirmationDialog(root, request)
            self.assertIs(dialog.confirm_button.master, dialog.button_bar)
            self.assertEqual(dialog.confirm_button.pack_info()["in"], dialog.button_bar)
            self.assertEqual(str(dialog.confirm_button["state"]), "disabled")
            dialog.typed.set(request.phrase)
            root.update_idletasks()
            self.assertEqual(str(dialog.confirm_button["state"]), "normal")
        finally:
            if dialog is not None and dialog.winfo_exists():
                dialog.destroy()
            root.destroy()

    def test_source_default_output_uses_project_root(self) -> None:
        self.assertEqual(default_output_parent(), application_root())

    def test_dpi_scale_and_window_size(self) -> None:
        self.assertEqual(dpi_scale(96), 1.0)
        self.assertEqual(dpi_scale(144), 1.5)
        self.assertEqual(scaled_window_size(3840, 2160, 192), (2080, 1560))
        self.assertEqual(scaled_window_size(1920, 1080, 144), (1560, 972))

    def test_interface_selection_extracts_global_index(self) -> None:
        self.assertEqual(interface_selection("[7] Realtek PCIe · 00:11:22:33:44:55"), "7")
        self.assertEqual(interface_selection("自动（全部物理网卡）"), "all")

    def test_automatic_network_fields_become_backend_auto_detection(self) -> None:
        self.assertIsNone(optional_manual_value(AUTO_REMOTE_TARGET, AUTO_REMOTE_TARGET))
        self.assertIsNone(optional_manual_value(AUTO_PC_HOST, AUTO_PC_HOST))
        self.assertIsNone(optional_manual_value("", AUTO_PC_HOST))
        self.assertEqual(optional_manual_value("192.168.68.10", AUTO_PC_HOST), "192.168.68.10")

    def test_output_path_is_timestamped_and_collision_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            instant = datetime(2026, 9, 6, 12, 34, 56)
            first = new_output_path(parent, "backup", instant)
            second = new_output_path(parent, "backup", instant)
            self.assertEqual(first.name, "Athena_AX6600_backup_20260906_123456")
            self.assertEqual(second.name, "Athena_AX6600_backup_20260906_123456_2")
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())


if __name__ == "__main__":
    unittest.main()
