"""Windows-friendly Tk GUI for the guarded Athena workflows."""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, Optional, Tuple

from .backup import AthenaBackupRunner, BackupOptions
from .constants import (
    DEFAULT_FACTORY_FIRMWARE_NAME,
    DEFAULT_HTTP_PORT,
    DEFAULT_MANAGEMENT_URL,
    DEFAULT_TELNET_PORT,
    DEFAULT_UBOOT_IMAGE_NAME,
    DEFAULT_UBOOT_WEB_URL,
    RAW_PREFIX_MIB,
    __version__,
)
from .errors import AthenaError, OperationCancelled
from .firmware_flash import FirmwareFlasher, FirmwareFlashPlan, discover_backup
from .flash import UbootFlashPlan
from .partition_resize import (
    ROOTFS_SIZE_CHOICES_MIB,
    RootfsResizePlan,
    RootfsResizer,
)
from .uboot_enter import InterfaceInfo, UbootEnterResult, UbootEnterService
from .util import normalize_management_url

LOGGER = logging.getLogger(__name__)


OPERATION_LABELS = {
    "backup": "自动备份 / 开启 Telnet",
    "flash-uboot": "刷写 U-Boot",
    "flash-firmware": "刷写 Factory 固件",
    "resize-rootfs": "扩容 rootfs + 刷固件",
    "enter-uboot": "进入 U-Boot Web",
}
AUTO_REMOTE_TARGET = "自动选择（U 盘优先，其次 /mnt/mmcblk0p27）"
AUTO_PC_HOST = "自动检测（根据路由器管理地址）"

BASE_DPI = 96
BASE_WINDOW_SIZE = (1040, 780)
BASE_MINIMUM_SIZE = (920, 700)
PER_MONITOR_AWARE_V2 = -4


def enable_windows_high_dpi() -> str:
    """Enable the best available Windows DPI mode before Tk creates a window."""

    if os.name != "nt":
        return "not-windows"
    try:
        user32 = ctypes.windll.user32
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_bool
        if setter(ctypes.c_void_p(PER_MONITOR_AWARE_V2)):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.windll.shcore
        setter = shcore.SetProcessDpiAwareness
        setter.argtypes = [ctypes.c_int]
        setter.restype = ctypes.c_long
        if setter(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    # A packaged executable may already be Per-Monitor V2 through its manifest.
    return "manifest-or-default"


def dpi_scale(dpi: int) -> float:
    """Convert a Windows DPI value to a bounded UI scale factor."""

    return min(max(float(dpi) / BASE_DPI, 0.75), 4.0)


def scaled_window_size(screen_width: int, screen_height: int, dpi: int) -> Tuple[int, int]:
    """Choose a DPI-scaled initial size that still fits the current display."""

    scale = dpi_scale(dpi)
    width = min(round(BASE_WINDOW_SIZE[0] * scale), round(screen_width * 0.94))
    height = min(round(BASE_WINDOW_SIZE[1] * scale), round(screen_height * 0.90))
    return max(width, 720), max(height, 560)


def window_dpi(root: tk.Tk) -> int:
    """Read the DPI for this window, falling back to Tk's screen measurement."""

    if os.name == "nt":
        try:
            getter = ctypes.windll.user32.GetDpiForWindow
            getter.argtypes = [ctypes.c_void_p]
            getter.restype = ctypes.c_uint
            detected = int(getter(root.winfo_id()))
            if detected > 0:
                return detected
        except (AttributeError, OSError, tk.TclError):
            pass
    try:
        return max(round(float(root.winfo_fpixels("1i"))), BASE_DPI)
    except tk.TclError:
        return BASE_DPI


def apply_tk_dpi(root: tk.Tk, dpi: int) -> None:
    """Keep Tk point units in sync with the current monitor."""

    root.tk.call("tk", "scaling", float(dpi) / 72.0)


def application_root() -> Path:
    """Return the source or bundled resource directory."""

    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(str(bundled)).resolve()
    return Path(__file__).resolve().parents[2]


def default_output_parent() -> Path:
    """Use the executable directory when bundled, or the project root from source."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return application_root()


def resource_path(filename: str) -> Path:
    """Locate a replaceable bundled asset without silently choosing a wrong file."""

    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / filename)
    candidates.extend((application_root() / filename, Path.cwd() / filename))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def new_output_path(parent: Path, operation: str, now: Optional[datetime] = None) -> Path:
    """Create a collision-free timestamped output path for one GUI run."""

    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    prefix = {
        "backup": "Athena_AX6600_backup",
        "flash-uboot": "Athena_AX6600_uboot_flash",
        "flash-firmware": "Athena_AX6600_firmware_flash",
        "resize-rootfs": "Athena_AX6600_rootfs_resize",
    }[operation]
    base = parent.expanduser().resolve() / f"{prefix}_{timestamp}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{suffix}")
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def interface_selection(display_value: str) -> str:
    """Turn ``[7] adapter`` from the GUI into the backend selection token."""

    value = display_value.strip()
    if not value or value == "自动（全部物理网卡）":
        return "all"
    if value.startswith("[") and "]" in value:
        index = value[1 : value.index("]")].strip()
        if index.isdigit():
            return index
    return value


def optional_manual_value(value: str, automatic_label: str) -> Optional[str]:
    """Return ``None`` for a blank/automatic GUI value, otherwise the manual value."""

    cleaned = value.strip()
    return None if not cleaned or cleaned == automatic_label else cleaned


def is_windows_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def npcap_installed() -> bool:
    """Perform a lightweight local Npcap installation check."""

    if os.name != "nt":
        return False
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    paths = (
        windows / "System32" / "Npcap" / "wpcap.dll",
        windows / "System32" / "Npcap" / "Packet.dll",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Npcap",
    )
    return any(path.exists() for path in paths)


@dataclass(frozen=True)
class OperationConfig:
    operation: str
    management_url: str
    telnet_port: int
    username: str
    password: str
    output_parent: Path
    backup_mode: str
    remote_target: Optional[str]
    pc_host: Optional[str]
    force_device: bool
    uboot_image: Path
    firmware_image: Path
    firmware_backup: Optional[Path]
    uboot_web_url: str
    interface: str
    enter_timeout: float
    uboot_http_timeout: float
    firmware_timeout: float
    open_browser: bool
    firmware_reboot: bool
    rootfs_size_mib: int
    verbose: bool


@dataclass
class ConfirmationRequest:
    title: str
    warning: str
    details: str
    phrase: str
    done: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False


@dataclass(frozen=True)
class RunResult:
    success: bool
    title: str
    message: str
    output: Optional[Path] = None
    cancelled: bool = False


class QueueLogHandler(logging.Handler):
    """Move worker-thread log records into Tk's main thread."""

    def __init__(self, events: "queue.Queue[Tuple[str, Any]]") -> None:
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put(("log", self.format(record)))
        except Exception:
            self.handleError(record)


class TypedConfirmationDialog(tk.Toplevel):
    """A modal dialog that only enables commit for the exact hash-bound phrase."""

    def __init__(self, parent: tk.Tk, request: ConfirmationRequest) -> None:
        super().__init__(parent)
        self.result = False
        self.request = request
        self.title(request.title)
        scale = dpi_scale(window_dpi(parent))
        self.geometry(f"{round(680 * scale)}x{round(500 * scale)}")
        self.minsize(round(600 * scale), round(440 * scale))
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="高风险写入确认", style="DangerTitle.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text=request.warning,
            style="Danger.TLabel",
            wraplength=620,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(8, 12))

        details_frame = ttk.Frame(body)
        details_frame.pack(fill="both", expand=True)
        details = tk.Text(
            details_frame,
            height=12,
            wrap="word",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=8,
            font=("Microsoft YaHei UI", 9),
        )
        details.insert("1.0", request.details)
        details.configure(state="disabled")
        details_scrollbar = ttk.Scrollbar(
            details_frame,
            orient="vertical",
            command=details.yview,
        )
        details.configure(yscrollcommand=details_scrollbar.set)
        details_scrollbar.pack(side="right", fill="y")
        details.pack(side="left", fill="both", expand=True)

        ttk.Label(body, text="请完整输入以下确认短语：").pack(anchor="w", pady=(14, 4))
        expected = ttk.Entry(body)
        expected.insert(0, request.phrase)
        expected.configure(state="readonly")
        expected.pack(fill="x")

        self.typed = tk.StringVar()
        entry = ttk.Entry(body, textvariable=self.typed, font=("Consolas", 10))
        entry.pack(fill="x", pady=(8, 14))
        self.button_bar = ttk.Frame(body)
        self.button_bar.pack(fill="x")
        ttk.Button(
            self.button_bar,
            text="取消（不写入）",
            command=self._cancel,
        ).pack(side="right")
        self.confirm_button = ttk.Button(
            self.button_bar,
            text="确认并开始写入",
            style="Danger.TButton",
            command=self._confirm,
            state="disabled",
        )
        self.confirm_button.pack(side="right", padx=(0, 8))
        self.typed.trace_add("write", self._validate)
        self.bind("<Escape>", lambda _event: self._cancel())
        entry.focus_set()
        self.grab_set()

    def _validate(self, *_args: object) -> None:
        state = "normal" if self.typed.get().strip() == self.request.phrase else "disabled"
        self.confirm_button.configure(state=state)

    def _confirm(self) -> None:
        if self.typed.get().strip() == self.request.phrase:
            self.result = True
            self.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.destroy()

    def show(self) -> bool:
        self.wait_window()
        return self.result


class AthenaGui:
    """Main application window."""

    def __init__(self, root: tk.Tk, dpi_mode: str = "automatic") -> None:
        self.root = root
        self.dpi_mode = dpi_mode
        self.current_dpi = window_dpi(root)
        apply_tk_dpi(root, self.current_dpi)
        width, height = scaled_window_size(
            root.winfo_screenwidth(),
            root.winfo_screenheight(),
            self.current_dpi,
        )
        min_scale = dpi_scale(self.current_dpi)
        self.root.title(f"JDBox Athena 工具 {__version__}")
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(
            min(round(BASE_MINIMUM_SIZE[0] * min_scale), width),
            min(round(BASE_MINIMUM_SIZE[1] * min_scale), height),
        )
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.running = False
        self.cancel_event = threading.Event()
        self.cancel_lock = threading.Lock()
        self.cancel_allowed = False
        self.last_output: Optional[Path] = None
        self.interface_items: Dict[str, InterfaceInfo] = {}

        self._create_variables()
        self._configure_style()
        self._build_window()
        self._install_logging()
        self._render_operation()
        self._refresh_environment()
        self.root.after(80, self._drain_events)
        self.root.after(1000, self._monitor_dpi)

    def _create_variables(self) -> None:
        self.operation = tk.StringVar(value="backup")
        self.management_url = tk.StringVar(value=DEFAULT_MANAGEMENT_URL)
        self.telnet_port = tk.StringVar(value=str(DEFAULT_TELNET_PORT))
        self.username = tk.StringVar(value="root")
        self.password = tk.StringVar()
        self.show_password = tk.BooleanVar(value=False)
        self.output_parent = tk.StringVar(value=str(default_output_parent()))
        self.backup_mode = tk.StringVar(value="split")
        self.remote_target = tk.StringVar(value=AUTO_REMOTE_TARGET)
        self.pc_host = tk.StringVar(value=AUTO_PC_HOST)
        self.force_device = tk.BooleanVar(value=False)
        self.uboot_image = tk.StringVar(value=str(resource_path(DEFAULT_UBOOT_IMAGE_NAME)))
        self.firmware_image = tk.StringVar(
            value=str(resource_path(DEFAULT_FACTORY_FIRMWARE_NAME))
        )
        self.firmware_backup = tk.StringVar()
        self.uboot_web_url = tk.StringVar(value=DEFAULT_UBOOT_WEB_URL)
        self.interface = tk.StringVar(value="自动（全部物理网卡）")
        self.enter_timeout = tk.StringVar(value="120")
        self.uboot_http_timeout = tk.StringVar(value="15")
        self.firmware_timeout = tk.StringVar(value="600")
        self.open_browser = tk.BooleanVar(value=True)
        self.firmware_reboot = tk.BooleanVar(value=False)
        self.rootfs_size = tk.StringVar(value="1024 MiB")
        self.verbose = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="就绪")
        self.admin_status = tk.StringVar()
        self.npcap_status = tk.StringVar()
        self.dpi_status = tk.StringVar()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Subtitle.TLabel", foreground="#58606b")
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure(
            "DangerTitle.TLabel",
            font=("Microsoft YaHei UI", 15, "bold"),
            foreground="#a51d21",
        )
        style.configure("Danger.TLabel", foreground="#a51d21")
        style.configure("Hint.TLabel", foreground="#58606b")
        style.configure("Success.TLabel", foreground="#176b3a")
        style.configure("Start.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(18, 9))
        style.configure("Danger.TButton", padding=(12, 7))

    def _build_window(self) -> None:
        outer = ttk.Frame(self.root, padding=(18, 14))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="JDBox Athena Windows 工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="雅典娜 AX6600（RE-CS-02）备份、U-Boot、rootfs 扩容与 Factory 固件受保护刷写",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        badges = ttk.Frame(header)
        badges.pack(anchor="e", side="right")
        ttk.Label(badges, textvariable=self.dpi_status).pack(side="left", padx=(0, 14))
        ttk.Label(badges, textvariable=self.admin_status).pack(side="left", padx=(0, 14))
        ttk.Label(badges, textvariable=self.npcap_status).pack(side="left")
        self.npcap_button = ttk.Button(badges, text="安装 Npcap", command=self._install_npcap)
        self.npcap_button.pack(side="left", padx=(8, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.task_container = ttk.Frame(self.notebook)
        self.log_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.task_container, text="  一键操作  ")
        self.notebook.add(self.log_tab, text="  运行日志  ")

        self.task_canvas = tk.Canvas(
            self.task_container,
            highlightthickness=0,
            borderwidth=0,
        )
        task_scrollbar = ttk.Scrollbar(
            self.task_container,
            orient="vertical",
            command=self.task_canvas.yview,
        )
        self.task_canvas.configure(yscrollcommand=task_scrollbar.set)
        task_scrollbar.pack(side="right", fill="y")
        self.task_canvas.pack(side="left", fill="both", expand=True)
        self.task_tab = ttk.Frame(self.task_canvas, padding=14)
        self.task_window = self.task_canvas.create_window(
            (0, 0),
            window=self.task_tab,
            anchor="nw",
        )
        self.task_tab.bind("<Configure>", self._update_task_scroll_region)
        self.task_canvas.bind("<Configure>", self._resize_task_content)
        self.task_canvas.bind("<MouseWheel>", self._scroll_task_page)
        self.task_tab.bind("<MouseWheel>", self._scroll_task_page)

        operations = ttk.LabelFrame(
            self.task_tab,
            text="1. 选择任务",
            style="Section.TLabelframe",
            padding=(12, 8),
        )
        operations.pack(fill="x")
        for index, (value, label) in enumerate(OPERATION_LABELS.items()):
            button = ttk.Radiobutton(
                operations,
                text=label,
                value=value,
                variable=self.operation,
                command=self._render_operation,
            )
            button.grid(
                row=index // 3,
                column=index % 3,
                sticky="w",
                padx=(0, 28),
                pady=3,
            )

        common = ttk.LabelFrame(
            self.task_tab,
            text="2. 基本设置",
            style="Section.TLabelframe",
            padding=12,
        )
        common.pack(fill="x", pady=(12, 0))
        common.columnconfigure(1, weight=1)
        common.columnconfigure(4, weight=1)
        ttk.Label(common, text="管理地址").grid(row=0, column=0, sticky="w", pady=4)
        self.management_entry = ttk.Entry(common, textvariable=self.management_url)
        self.management_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 18), pady=4)
        ttk.Label(common, text="Telnet 端口").grid(row=0, column=3, sticky="w", pady=4)
        self.telnet_entry = ttk.Entry(common, textvariable=self.telnet_port, width=9)
        self.telnet_entry.grid(row=0, column=4, sticky="w", padx=(8, 0), pady=4)

        ttk.Label(common, text="用户名").grid(row=1, column=0, sticky="w", pady=4)
        self.user_entry = ttk.Entry(common, textvariable=self.username)
        self.user_entry.grid(row=1, column=1, sticky="ew", padx=(8, 18), pady=4)
        ttk.Label(common, text="管理员密码（首次配置时设置）").grid(
            row=1,
            column=2,
            sticky="w",
            pady=4,
        )
        self.password_entry = ttk.Entry(common, textvariable=self.password, show="●")
        self.password_entry.grid(row=1, column=3, columnspan=2, sticky="ew", padx=(8, 0), pady=4)
        ttk.Checkbutton(
            common,
            text="显示",
            variable=self.show_password,
            command=self._toggle_password,
        ).grid(row=1, column=5, padx=(8, 0))

        ttk.Label(common, text="输出父目录").grid(row=2, column=0, sticky="w", pady=4)
        self.output_entry = ttk.Entry(common, textvariable=self.output_parent)
        self.output_entry.grid(row=2, column=1, columnspan=4, sticky="ew", padx=(8, 8), pady=4)
        self.output_browse = ttk.Button(
            common,
            text="选择…",
            command=lambda: self._choose_directory(self.output_parent),
        )
        self.output_browse.grid(row=2, column=5, pady=4)

        self.details = ttk.LabelFrame(
            self.task_tab,
            text="3. 任务设置",
            style="Section.TLabelframe",
            padding=12,
        )
        self.details.pack(fill="x", pady=(12, 0))

        warning = ttk.Frame(self.task_tab, padding=(10, 8))
        warning.pack(fill="x", pady=(12, 0))
        self.warning_label = ttk.Label(
            warning,
            style="Danger.TLabel",
            wraplength=920,
            justify="left",
        )
        self.warning_label.pack(anchor="w", fill="x")

        actions = ttk.Frame(self.task_tab)
        actions.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=240)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 12))
        ttk.Label(actions, textvariable=self.status, width=32).pack(side="left", padx=(0, 12))
        self.start_button = ttk.Button(
            actions,
            text="开始执行",
            style="Start.TButton",
            command=self._start,
        )
        self.start_button.pack(side="right")
        self.cancel_button = ttk.Button(
            actions,
            text="取消当前任务",
            command=self._cancel_current,
            state="disabled",
        )
        self.cancel_button.pack(side="right", padx=(0, 8))

        log_actions = ttk.Frame(self.log_tab)
        log_actions.pack(fill="x", pady=(0, 8))
        ttk.Label(
            log_actions,
            text="执行记录（密码不会写入日志）",
            style="Hint.TLabel",
        ).pack(side="left")
        ttk.Button(log_actions, text="清空", command=self._clear_log).pack(side="right")
        self.open_output_button = ttk.Button(
            log_actions,
            text="打开输出目录",
            command=self._open_output,
            state="disabled",
        )
        self.open_output_button.pack(side="right", padx=(0, 8))
        self.log = tk.Text(
            self.log_tab,
            wrap="word",
            state="disabled",
            background="#111827",
            foreground="#dbe4f0",
            insertbackground="white",
            font=("Consolas", 9),
            padx=10,
            pady=10,
        )
        scrollbar = ttk.Scrollbar(self.log_tab, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

    def _install_logging(self) -> None:
        self.log_handler = QueueLogHandler(self.events)
        self.log_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
        )
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(self.log_handler)

    def _update_task_scroll_region(self, _event: tk.Event[tk.Misc]) -> None:
        self.task_canvas.configure(scrollregion=self.task_canvas.bbox("all"))

    def _resize_task_content(self, event: tk.Event[tk.Misc]) -> None:
        self.task_canvas.itemconfigure(self.task_window, width=event.width)

    def _scroll_task_page(self, event: tk.Event[tk.Misc]) -> str:
        delta = int(-event.delta / 120) if event.delta else 0
        if delta:
            self.task_canvas.yview_scroll(delta, "units")
        return "break"

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_password.get() else "●")

    def _set_common_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (
            self.management_entry,
            self.telnet_entry,
            self.user_entry,
            self.password_entry,
        ):
            widget.configure(state=state)

    def _add_entry(
        self,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        kind: Optional[str] = None,
        hint: Optional[str] = None,
    ) -> ttk.Entry:
        ttk.Label(self.details, text=label).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(self.details, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", padx=(10, 8), pady=5)
        if kind:
            command = (
                (lambda: self._choose_file(variable))
                if kind == "file"
                else (lambda: self._choose_directory(variable))
            )
            ttk.Button(self.details, text="选择…", command=command).grid(
                row=row, column=2, sticky="e", pady=5
            )
        if hint:
            ttk.Label(self.details, text=hint, style="Hint.TLabel").grid(
                row=row + 1, column=1, columnspan=2, sticky="w", padx=(10, 0)
            )
        return entry

    def _render_operation(self) -> None:
        for child in self.details.winfo_children():
            child.destroy()
        self.details.columnconfigure(1, weight=1)
        operation = self.operation.get()
        needs_login = operation in {"backup", "flash-uboot"}
        self._set_common_state(needs_login)
        output_state = "normal" if operation != "enter-uboot" else "disabled"
        self.output_entry.configure(state=output_state)
        self.output_browse.configure(state=output_state)

        if operation == "backup":
            ttk.Label(self.details, text="备份模式").grid(row=0, column=0, sticky="w", pady=5)
            ttk.Combobox(
                self.details,
                textvariable=self.backup_mode,
                values=("split", "raw", "both"),
                state="readonly",
                width=16,
            ).grid(row=0, column=1, sticky="w", padx=(10, 8), pady=5)
            ttk.Label(
                self.details,
                text=f"split：GPT+p1-p26（推荐）　raw：前 {RAW_PREFIX_MIB} MiB　both：两者",
                style="Hint.TLabel",
            ).grid(row=0, column=1, sticky="e", padx=(180, 8))
            self._add_entry(1, "路由器临时挂载点", self.remote_target)
            self._add_entry(2, "电脑局域网 IP", self.pc_host)
            ttk.Checkbutton(
                self.details,
                text="仅在人工确认分区标签差异后绕过 APPSBL/ART 标签保护",
                variable=self.force_device,
            ).grid(row=3, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=5)
            warning = (
                "只在你拥有或获准管理的设备上使用。默认 split 备份不会写分区；工具可能利用"
                "原厂管理接口自动开启 Telnet，建议网线直连且暂时断开互联网。"
            )
        elif operation == "flash-uboot":
            self._add_entry(0, "U-Boot 镜像", self.uboot_image, kind="file")
            self._add_entry(1, "路由器临时挂载点", self.remote_target)
            self._add_entry(2, "电脑局域网 IP", self.pc_host)
            warning = (
                "将先备份 p13/p14，再严格校验设备、固定镜像哈希、远端上传和回读结果。"
                "最终写入前必须输入哈希绑定确认短语；软件不会自动重启。写入期间绝对不要断电。"
            )
        elif operation in {"flash-firmware", "resize-rootfs"}:
            row = 0
            if operation == "resize-rootfs":
                ttk.Label(self.details, text="rootfs 目标大小").grid(
                    row=row, column=0, sticky="w", pady=5
                )
                ttk.Combobox(
                    self.details,
                    textvariable=self.rootfs_size,
                    values=tuple(f"{value} MiB" for value in ROOTFS_SIZE_CHOICES_MIB),
                    state="readonly",
                    width=16,
                ).grid(row=row, column=1, sticky="w", padx=(10, 8), pady=5)
                row += 1
            self._add_entry(row, "Factory 固件", self.firmware_image, kind="file")
            self._add_entry(row + 1, "完整 split 备份", self.firmware_backup, kind="directory")
            self._add_entry(row + 2, "U-Boot Web 地址", self.uboot_web_url)
            self._add_interface_row(row + 3)
            ttk.Label(self.details, text="最长请求秒数").grid(
                row=row + 4, column=0, sticky="w", pady=5
            )
            ttk.Entry(self.details, textvariable=self.firmware_timeout, width=12).grid(
                row=row + 4, column=1, sticky="w", padx=(10, 8), pady=5
            )
            ttk.Checkbutton(
                self.details,
                text="刷写成功后自动重启（默认关闭）",
                variable=self.firmware_reboot,
            ).grid(row=row + 5, column=1, sticky="w", padx=(10, 0), pady=5)
            if operation == "resize-rootfs":
                warning = (
                    "高风险：GPT 写入会顺移 p19-p26 并破坏 rootfs_data、plugin、log、swap、"
                    "storage 的现有数据。必须使用本机完整备份，随后会连续刷入 Factory 固件；"
                    "两次写入都需输入独立确认短语。"
                )
            else:
                warning = (
                    "若 U-Boot Web 未就绪，软件会用所选网卡等待上电/重启并中断启动。"
                    "固件上传到内存并二次校验后仍会要求输入确认短语；提交写入后禁止断电或重复提交。"
                )
        else:
            self._add_interface_row(0)
            ttk.Label(self.details, text="等待路由器秒数").grid(row=1, column=0, sticky="w", pady=5)
            ttk.Entry(self.details, textvariable=self.enter_timeout, width=12).grid(
                row=1, column=1, sticky="w", padx=(10, 8), pady=5
            )
            ttk.Label(self.details, text="等待 Web 秒数").grid(row=2, column=0, sticky="w", pady=5)
            ttk.Entry(self.details, textvariable=self.uboot_http_timeout, width=12).grid(
                row=2, column=1, sticky="w", padx=(10, 8), pady=5
            )
            ttk.Checkbutton(
                self.details,
                text="成功后打开默认浏览器",
                variable=self.open_browser,
            ).grid(row=3, column=1, sticky="w", padx=(10, 0), pady=5)
            warning = (
                "需要 Scapy、Npcap，部分电脑还需要管理员权限。点击开始后再给路由器上电或重启；"
                "该操作只中断兼容 U-Boot 的自动启动，不会主动重启设备。"
            )
        ttk.Checkbutton(self.details, text="详细日志", variable=self.verbose).grid(
            row=20, column=1, sticky="w", padx=(10, 0), pady=(7, 0)
        )
        self.warning_label.configure(text="⚠ " + warning)

    def _add_interface_row(self, row: int) -> None:
        ttk.Label(self.details, text="有线网卡").grid(row=row, column=0, sticky="w", pady=5)
        self.interface_combo = ttk.Combobox(
            self.details,
            textvariable=self.interface,
            values=("自动（全部物理网卡）",),
            state="readonly",
        )
        self.interface_combo.grid(row=row, column=1, sticky="ew", padx=(10, 8), pady=5)
        self.refresh_interfaces_button = ttk.Button(
            self.details,
            text="刷新网卡",
            command=self._refresh_interfaces,
        )
        self.refresh_interfaces_button.grid(row=row, column=2, sticky="e", pady=5)

    def _choose_file(self, variable: tk.StringVar) -> None:
        initial = Path(variable.get()).expanduser()
        selected = filedialog.askopenfilename(
            parent=self.root,
            initialdir=str(initial.parent if initial.parent.exists() else Path.cwd()),
            filetypes=(("固件镜像", "*.bin"), ("所有文件", "*.*")),
        )
        if selected:
            variable.set(selected)

    def _choose_directory(self, variable: tk.StringVar) -> None:
        initial = Path(variable.get()).expanduser() if variable.get().strip() else Path.cwd()
        if not initial.is_dir():
            initial = initial.parent
        selected = filedialog.askdirectory(parent=self.root, initialdir=str(initial))
        if selected:
            variable.set(selected)

    def _refresh_environment(self) -> None:
        admin = is_windows_admin()
        npcap = npcap_installed()
        self.dpi_status.set(f"缩放：{round(dpi_scale(self.current_dpi) * 100)}%")
        self.admin_status.set("管理员：是" if admin else "管理员：否")
        self.npcap_status.set("Npcap：已检测" if npcap else "Npcap：未检测")
        self.npcap_button.configure(state="disabled" if npcap else "normal")

    def _monitor_dpi(self) -> None:
        """Refresh Tk scaling after the window moves between monitors."""

        try:
            detected = window_dpi(self.root)
            if detected != self.current_dpi:
                self.current_dpi = detected
                apply_tk_dpi(self.root, detected)
                self.dpi_status.set(f"缩放：{round(dpi_scale(detected) * 100)}%")
                self.root.event_generate("<<ThemeChanged>>")
        finally:
            self.root.after(1000, self._monitor_dpi)

    def _install_npcap(self) -> None:
        installer = resource_path("npcap-1.88.exe")
        if not installer.is_file():
            messagebox.showerror("找不到安装包", f"未找到 Npcap 安装包：\n{installer}")
            return
        if os.name != "nt":
            messagebox.showinfo("仅限 Windows", "Npcap 安装包只能在 Windows 上运行。")
            return
        if not messagebox.askyesno(
            "安装 Npcap",
            "将启动项目附带的 Npcap 安装程序，并显示 Windows UAC 提示。是否继续？",
            parent=self.root,
        ):
            return
        try:
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", str(installer), None, str(installer.parent), 1
            )
            if int(result) <= 32:
                raise OSError(f"ShellExecuteW 返回 {result}")
        except (AttributeError, OSError) as exc:
            messagebox.showerror("启动失败", f"无法启动 Npcap 安装程序：{exc}")

    def _refresh_interfaces(self) -> None:
        if self.running:
            return
        self.status.set("正在读取物理网卡…")
        if hasattr(self, "refresh_interfaces_button"):
            self.refresh_interfaces_button.configure(state="disabled")

        def worker() -> None:
            try:
                items = UbootEnterService().list_interfaces()
                self.events.put(("interfaces", items))
            except Exception as exc:
                self.events.put(("interface-error", str(exc)))

        threading.Thread(target=worker, name="athena-interface-scan", daemon=True).start()

    @staticmethod
    def _parse_positive(value: str, label: str, *, maximum: Optional[int] = None) -> int:
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise AthenaError(f"{label}必须是整数。") from exc
        if parsed <= 0 or (maximum is not None and parsed > maximum):
            suffix = f"且不大于 {maximum}" if maximum is not None else ""
            raise AthenaError(f"{label}必须大于 0{suffix}。")
        return parsed

    def _capture_config(self) -> OperationConfig:
        operation = self.operation.get()
        output_text = self.output_parent.get().strip()
        if operation != "enter-uboot" and not output_text:
            raise AthenaError("请选择输出父目录。")
        needs_login = operation in {"backup", "flash-uboot"}
        management_url = self.management_url.get().strip()
        if needs_login:
            normalize_management_url(management_url)
            if not self.username.get().strip():
                raise AthenaError("用户名不能为空。")
            if not self.password.get():
                raise AthenaError(
                    "请输入首次配置时设置的路由器管理员密码；"
                    "京东云官方没有统一的出厂管理密码。"
                )
        telnet_port = self._parse_positive(self.telnet_port.get(), "Telnet 端口", maximum=65535)
        enter_timeout = self._parse_positive(self.enter_timeout.get(), "等待路由器秒数")
        http_timeout = self._parse_positive(self.uboot_http_timeout.get(), "等待 Web 秒数")
        firmware_timeout = self._parse_positive(self.firmware_timeout.get(), "请求超时秒数")

        uboot_image = Path(self.uboot_image.get()).expanduser().resolve()
        firmware_image = Path(self.firmware_image.get()).expanduser().resolve()
        backup_text = self.firmware_backup.get().strip()
        firmware_backup = Path(backup_text).expanduser().resolve() if backup_text else None
        if operation == "flash-uboot" and not uboot_image.is_file():
            raise AthenaError(f"找不到 U-Boot 镜像：{uboot_image}")
        if operation in {"flash-firmware", "resize-rootfs"}:
            if not firmware_image.is_file():
                raise AthenaError(f"找不到 Factory 固件：{firmware_image}")
            if firmware_backup is not None and not firmware_backup.is_dir():
                raise AthenaError(f"找不到 split 备份目录：{firmware_backup}")

        try:
            rootfs_size_mib = int(self.rootfs_size.get().split()[0])
        except (IndexError, ValueError) as exc:
            raise AthenaError("rootfs 目标大小无效。") from exc
        if rootfs_size_mib not in ROOTFS_SIZE_CHOICES_MIB:
            raise AthenaError("rootfs 目标大小必须选择 512、1024、2048 或 8192 MiB。")

        return OperationConfig(
            operation=operation,
            management_url=management_url,
            telnet_port=telnet_port,
            username=self.username.get().strip(),
            password=self.password.get(),
            output_parent=Path(output_text).expanduser().resolve() if output_text else Path.cwd(),
            backup_mode=self.backup_mode.get(),
            remote_target=optional_manual_value(
                self.remote_target.get(),
                AUTO_REMOTE_TARGET,
            ),
            pc_host=optional_manual_value(self.pc_host.get(), AUTO_PC_HOST),
            force_device=bool(self.force_device.get()),
            uboot_image=uboot_image,
            firmware_image=firmware_image,
            firmware_backup=firmware_backup,
            uboot_web_url=self.uboot_web_url.get().strip(),
            interface=interface_selection(self.interface.get()),
            enter_timeout=float(enter_timeout),
            uboot_http_timeout=float(http_timeout),
            firmware_timeout=float(firmware_timeout),
            open_browser=bool(self.open_browser.get()),
            firmware_reboot=bool(self.firmware_reboot.get()),
            rootfs_size_mib=rootfs_size_mib,
            verbose=bool(self.verbose.get()),
        )

    def _start(self) -> None:
        if self.running:
            return
        try:
            config = self._capture_config()
        except (AthenaError, OSError, ValueError) as exc:
            messagebox.showerror("设置有误", str(exc), parent=self.root)
            return
        risky_operations = {"flash-uboot", "flash-firmware", "resize-rootfs"}
        if config.operation in risky_operations and not messagebox.askyesno(
            "确认启动预检",
            "软件将先执行只读检查和备份校验。真正写入前还会要求输入哈希绑定确认短语。\n\n"
            "请确认设备使用稳定电源并通过网线连接。是否开始？",
            icon="warning",
            parent=self.root,
        ):
            return
        self.running = True
        with self.cancel_lock:
            self.cancel_event.clear()
            self.cancel_allowed = False
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        self.progress.start(12)
        self.status.set("正在启动任务…")
        self.notebook.select(self.log_tab)
        self._append_log(
            f"{datetime.now():%H:%M:%S} INFO 开始：{OPERATION_LABELS[config.operation]}"
        )
        logging.getLogger().setLevel(logging.DEBUG if config.verbose else logging.INFO)
        threading.Thread(
            target=self._run_worker,
            args=(config,),
            name="athena-workflow",
            daemon=True,
        ).start()

    def _cancel_current(self) -> None:
        with self.cancel_lock:
            if not self.running or not self.cancel_allowed or self.cancel_event.is_set():
                return
            self.cancel_event.set()
            self.cancel_allowed = False
        self.cancel_button.configure(state="disabled")
        self.status.set("正在安全取消…")
        self._append_log(f"{datetime.now():%H:%M:%S} INFO 用户请求取消等待 U-Boot")

    def _enter_uboot(
        self,
        config: OperationConfig,
        *,
        open_browser: bool,
    ) -> UbootEnterResult:
        with self.cancel_lock:
            if self.cancel_event.is_set():
                raise OperationCancelled("任务已取消。")
            self.cancel_allowed = True
        self.events.put(("cancellable", True))
        try:
            result = UbootEnterService().run(
                config.interface,
                timeout=config.enter_timeout,
                http_timeout=config.uboot_http_timeout,
                open_browser=open_browser,
                cancel_event=self.cancel_event,
            )
        finally:
            with self.cancel_lock:
                cancelled = self.cancel_event.is_set()
                self.cancel_allowed = False
            self.events.put(("cancellable", False))
            if cancelled:
                raise OperationCancelled(
                    "已取消等待 U-Boot 启动，未执行后续固件写入。"
                    "如果已经收到 UBOOT:ABORTED，路由器可能仍停留在 U-Boot；"
                    "请手动访问管理地址或重启路由器。"
                )
        return result

    def _backup_options(
        self,
        config: OperationConfig,
        operation: str,
        output: Path,
    ) -> BackupOptions:
        management_url, router_host = normalize_management_url(config.management_url)
        return BackupOptions(
            management_url=management_url,
            router_host=router_host,
            telnet_port=config.telnet_port,
            username=config.username,
            password=config.password,
            operation=operation,
            mode=config.backup_mode,
            output=output,
            uboot_image=config.uboot_image,
            remote_target=config.remote_target,
            http_port=DEFAULT_HTTP_PORT,
            pc_host=config.pc_host,
            listen_host="0.0.0.0",
            stream_port=0,
            force_device=config.force_device if operation == "backup" else False,
            rpc_timeout=8.0,
            telnet_wait=20.0,
            command_timeout=7200.0,
            raw_connect_timeout=45.0,
        )

    def _run_worker(self, config: OperationConfig) -> None:
        output: Optional[Path] = None
        try:
            if config.operation == "backup":
                output = new_output_path(config.output_parent, "backup")
                options = self._backup_options(config, "backup", output)
                artifacts = AthenaBackupRunner(options).run()
                result = RunResult(
                    True,
                    "备份完成",
                    f"已完成并校验 {len(artifacts)} 个镜像文件。",
                    output,
                )
            elif config.operation == "flash-uboot":
                output = new_output_path(config.output_parent, "flash-uboot")
                options = self._backup_options(config, "flash-uboot", output)
                artifacts = AthenaBackupRunner(
                    options,
                    confirm_uboot=self._confirm_uboot,
                ).run()
                result = RunResult(
                    True,
                    "U-Boot 刷写完成",
                    f"双 APPSBL 已写入并回读校验；保留了 {len(artifacts)} 个刷写前备份。"
                    "软件没有自动重启路由器。",
                    output,
                )
            elif config.operation == "flash-firmware":
                output = new_output_path(config.output_parent, "flash-firmware")
                backup = discover_backup(
                    config.firmware_backup,
                    (application_root(), Path.cwd(), config.output_parent),
                )
                flasher = FirmwareFlasher(
                    config.uboot_web_url,
                    output,
                    config.firmware_image,
                    backup,
                    timeout=config.firmware_timeout,
                )
                if flasher.probe_version(required=False) is None:
                    LOGGER.info("U-Boot Web 尚未就绪，启动网卡中断流程")
                    entered = self._enter_uboot(config, open_browser=False)
                    flasher = FirmwareFlasher(
                        entered.web_url,
                        output,
                        config.firmware_image,
                        backup,
                        timeout=config.firmware_timeout,
                    )
                report = flasher.flash(
                    self._confirm_firmware,
                    auto_reboot=config.firmware_reboot,
                )
                ending = (
                    "路由器将自动重启。"
                    if config.firmware_reboot
                    else "路由器仍停留在 U-Boot Web。"
                )
                result = RunResult(
                    True,
                    "Factory 固件刷写完成",
                    f"U-Boot 已返回写入成功，报告：{report.name}。{ending}",
                    output,
                )
            elif config.operation == "resize-rootfs":
                output = new_output_path(config.output_parent, "resize-rootfs")
                backup = discover_backup(
                    config.firmware_backup,
                    (application_root(), Path.cwd(), config.output_parent),
                )
                resizer = RootfsResizer(
                    config.uboot_web_url,
                    output,
                    backup,
                    config.rootfs_size_mib,
                    timeout=config.firmware_timeout,
                )
                backup_info, generated = resizer.prepare()
                flasher = FirmwareFlasher(
                    config.uboot_web_url,
                    output,
                    config.firmware_image,
                    backup,
                    timeout=config.firmware_timeout,
                )
                # Do all local firmware checks before the first destructive write.
                flasher.validate_image()
                flasher.validate_backup()
                if resizer.probe_version(required=False) is None:
                    LOGGER.info("U-Boot Web 尚未就绪，启动网卡中断流程")
                    entered = self._enter_uboot(config, open_browser=False)
                    resizer.set_web_url(entered.web_url)
                    flasher = FirmwareFlasher(
                        entered.web_url,
                        output,
                        config.firmware_image,
                        backup,
                        timeout=config.firmware_timeout,
                    )
                resize_report = resizer.commit(
                    backup_info,
                    generated,
                    self._confirm_resize,
                )
                LOGGER.warning(
                    "GPT 已写入且未重启；继续刷写 Factory 固件。"
                    "若后续失败，请保持在 U-Boot 并重试固件刷写。"
                )
                firmware_report = flasher.flash(
                    self._confirm_firmware,
                    auto_reboot=config.firmware_reboot,
                )
                ending = (
                    "路由器将自动重启。"
                    if config.firmware_reboot
                    else "路由器仍停留在 U-Boot Web。"
                )
                result = RunResult(
                    True,
                    "rootfs 扩容与固件刷写完成",
                    f"rootfs 已调整为 {config.rootfs_size_mib} MiB；"
                    f"报告：{resize_report.name}、{firmware_report.name}。{ending}",
                    output,
                )
            else:
                entered = self._enter_uboot(config, open_browser=config.open_browser)
                result = RunResult(
                    True,
                    "已进入 U-Boot Web",
                    f"地址：{entered.web_url}\n版本：{entered.version}\n"
                    f"发送 {entered.attempts} 轮，耗时 {entered.elapsed_seconds:.1f} 秒。",
                )
        except OperationCancelled as exc:
            LOGGER.info("%s", exc)
            result = RunResult(False, "任务已取消", str(exc), output, cancelled=True)
        except (AthenaError, OSError, ValueError) as exc:
            LOGGER.error("%s", exc, exc_info=config.verbose)
            result = RunResult(False, "任务未完成", str(exc), output)
        except Exception as exc:
            LOGGER.error("发生未预期错误：%s", exc)
            if config.verbose:
                LOGGER.debug("%s", traceback.format_exc())
            result = RunResult(False, "任务未完成", f"发生未预期错误：{exc}", output)
        self.events.put(("finished", result))

    def _request_confirmation(self, request: ConfirmationRequest) -> bool:
        self.events.put(("confirm", request))
        request.done.wait()
        return request.accepted

    def _confirm_uboot(self, plan: UbootFlashPlan) -> bool:
        details = (
            f"设备：{plan.model}\n"
            f"镜像：{plan.image.path}\n"
            f"SHA256：{plan.image.sha256}\n"
            f"写入顺序：{' → '.join(plan.write_order)}\n"
            f"刷写前备份：{', '.join(plan.backup_files.values())}\n\n"
            "确认后将先写备用 APPSBL，再写主 APPSBL，并分别回读校验。"
        )
        return self._request_confirmation(
            ConfirmationRequest(
                title="确认刷写 U-Boot",
                warning="断电、连接中断或错误设备可能导致路由器无法启动。写入期间绝对不要断电。",
                details=details,
                phrase=plan.confirmation_phrase,
            )
        )

    def _confirm_firmware(self, plan: FirmwareFlashPlan) -> bool:
        details = (
            f"镜像：{plan.image.path}\n"
            f"SHA256：{plan.image.sha256}\n"
            f"U-Boot：{plan.web_url}（{plan.uboot_version}）\n"
            f"内存校验：{dict(plan.upload_info)}\n"
            f"写入目标：{' → '.join(plan.write_targets)}\n"
            f"已验证恢复备份：{plan.backup.path}\n"
            f"刷写后自动重启：{'是' if plan.auto_reboot else '否'}\n\n"
            "GPT、U-Boot、ART 和系统 1 不会被写入。"
        )
        return self._request_confirmation(
            ConfirmationRequest(
                title="确认刷写 Factory 固件",
                warning="确认后会覆盖系统 0 的内核与 rootfs。提交后禁止断电或重复提交。",
                details=details,
                phrase=plan.confirmation_phrase,
            )
        )

    def _confirm_resize(self, plan: RootfsResizePlan) -> bool:
        generated = plan.generated_gpt
        change_lines = []
        for change in generated.changes:
            change_lines.append(
                f"p{change.number} {change.label}: "
                f"{change.old_first_lba}-{change.old_last_lba} → "
                f"{change.new_first_lba}-{change.new_last_lba}"
            )
        details = (
            f"设备专属 GPT：{generated.path}\n"
            f"SHA256：{generated.sha256}\n"
            f"U-Boot：{plan.web_url}（{plan.uboot_version}）\n"
            f"内存校验：{dict(plan.upload_info)}\n"
            f"恢复备份：{plan.backup.path}（已校验 {len(plan.backup.verified_files)} 个文件）\n"
            f"rootfs：{generated.rootfs_old_mib} MiB → {generated.rootfs_new_mib} MiB\n"
            f"storage：{generated.storage_old_mib:.2f} MiB → "
            f"{generated.storage_new_mib:.2f} MiB\n\n"
            "分区位置变化：\n" + "\n".join(change_lines) + "\n\n"
            "p1-p17 的位置和全部分区 GUID 保持不变；确认后写主/备 GPT，且不会自动重启。"
        )
        return self._request_confirmation(
            ConfirmationRequest(
                title="确认调整 rootfs 分区",
                warning=(
                    "写入 GPT 会使 p19-p27 的原有文件系统/数据不可识别，包括原厂系统 1、"
                    "rootfs_data、plugin、log、swap 和 storage。写入期间绝对不要断电。"
                ),
                details=details,
                phrase=plan.confirmation_phrase,
            )
        )

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    self._append_log(str(payload))
                    summary = str(payload).split(" ", 2)[-1]
                    self.status.set(summary[:46])
                elif event == "confirm":
                    request = payload
                    assert isinstance(request, ConfirmationRequest)
                    self.progress.stop()
                    self.status.set("等待写入确认")
                    dialog = TypedConfirmationDialog(self.root, request)
                    request.accepted = dialog.show()
                    request.done.set()
                    if self.running:
                        self.progress.start(12)
                        self.status.set("已确认，继续执行…" if request.accepted else "已取消写入")
                elif event == "cancellable":
                    enabled = bool(payload) and self.running and not self.cancel_event.is_set()
                    self.cancel_button.configure(state="normal" if enabled else "disabled")
                elif event == "interfaces":
                    items = payload
                    values = ["自动（全部物理网卡）"]
                    self.interface_items.clear()
                    for item in items:
                        display = f"[{item.index}] {item.description or item.name} · {item.mac}"
                        self.interface_items[display] = item
                        values.append(display)
                    if (
                        hasattr(self, "interface_combo")
                        and self.interface_combo.winfo_exists()
                    ):
                        self.interface_combo.configure(values=values)
                    if self.interface.get() not in values:
                        self.interface.set(values[0])
                    if (
                        hasattr(self, "refresh_interfaces_button")
                        and self.refresh_interfaces_button.winfo_exists()
                    ):
                        self.refresh_interfaces_button.configure(state="normal")
                    self.status.set(f"已找到 {len(items)} 个物理网卡")
                elif event == "interface-error":
                    if (
                        hasattr(self, "refresh_interfaces_button")
                        and self.refresh_interfaces_button.winfo_exists()
                    ):
                        self.refresh_interfaces_button.configure(state="normal")
                    self.status.set("网卡读取失败")
                    messagebox.showerror("无法读取网卡", str(payload), parent=self.root)
                elif event == "finished":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def _finish(self, result: RunResult) -> None:
        self.running = False
        self.progress.stop()
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        with self.cancel_lock:
            self.cancel_allowed = False
        self.status.set("完成" if result.success else ("已取消" if result.cancelled else "未完成"))
        if result.output is not None:
            self.last_output = result.output
            self.open_output_button.configure(state="normal")
        if result.success or result.cancelled:
            messagebox.showinfo(result.title, result.message, parent=self.root)
        else:
            messagebox.showerror(result.title, result.message, parent=self.root)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _open_output(self) -> None:
        if self.last_output is None or not self.last_output.exists():
            return
        try:
            if os.name == "nt":
                os.startfile(str(self.last_output))
            else:
                webbrowser.open(self.last_output.as_uri())
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.root)

    def _close(self) -> None:
        if self.running:
            messagebox.showwarning(
                "任务正在执行",
                "当前任务仍在执行，窗口暂不能关闭。等待 U-Boot 时可先点击“取消当前任务”；"
                "刷写阶段不要断电。",
                parent=self.root,
            )
            return
        logging.getLogger().removeHandler(self.log_handler)
        self.root.destroy()


def main() -> int:
    """Start the native Windows GUI."""

    dpi_mode = enable_windows_high_dpi()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        return 1
    AthenaGui(root, dpi_mode=dpi_mode)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
