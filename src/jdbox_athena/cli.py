"""Command-line interface for the Athena AX6600 backup workflow."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

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
from .errors import AthenaError
from .firmware_flash import (
    FirmwareFlasher,
    FirmwareFlashPlan,
    discover_backup,
)
from .flash import UbootFlashPlan
from .uboot_enter import UbootEnterService, format_interfaces
from .util import normalize_management_url, resolved_output

LOGGER = logging.getLogger(__name__)


def positive_int(value: str) -> int:
    """argparse type for positive TCP ports and timeouts."""

    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def tcp_port(value: str, *, allow_zero: bool = False) -> int:
    """Parse a TCP port, optionally accepting zero for an ephemeral port."""

    parsed = int(value)
    lower = 0 if allow_zero else 1
    if parsed < lower or parsed > 65535:
        raise argparse.ArgumentTypeError(f"端口必须在 {lower} 到 65535 之间")
    return parsed


def create_parser() -> argparse.ArgumentParser:
    """Build the public CLI parser."""

    parser = argparse.ArgumentParser(
        prog="athena_backup.py",
        description=(
            "京东云雅典娜 AX6600：自动检测/开启 Telnet，备份 GPT+p1-p26，"
            "受保护地刷写锁定 U-Boot/Factory 固件，或通过网络进入 U-Boot Web。"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        "--host",
        dest="management_url",
        default=DEFAULT_MANAGEMENT_URL,
        help="路由器管理地址；--host 是兼容别名，也可只传 IP",
    )
    parser.add_argument("--telnet-port", type=tcp_port, default=DEFAULT_TELNET_PORT)
    parser.add_argument("--user", default="root", help="后台与 Telnet 用户名")
    parser.add_argument(
        "--password",
        help="后台/Telnet 密码；省略时安全提示输入（命令行参数可能被历史记录保存）",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--mode",
        choices=("split", "raw", "both"),
        default="split",
        help=f"split=GPT+p1-p26；raw=前 {RAW_PREFIX_MIB} MiB；both=两者",
    )
    operation.add_argument(
        "--flash-uboot",
        action="store_true",
        help="独立执行 U-Boot 双 APPSBL 备份、刷写和回读校验（不会自动重启）",
    )
    operation.add_argument(
        "--flash-firmware",
        nargs="?",
        const="all",
        metavar="INTERFACE",
        help=(
            "通过 U-Boot Web 刷写锁定的 Factory 固件；可传 uBootEnter 网卡索引/名称，"
            "省略值时使用全部物理网卡"
        ),
    )
    operation.add_argument(
        "--enter-uboot",
        nargs="?",
        const="all",
        metavar="INTERFACE",
        help="发送 uBootEnter 中断包；可传网卡索引/名称，省略值时使用全部物理网卡",
    )
    operation.add_argument(
        "--list-interfaces",
        action="store_true",
        help="列出 uBootEnter 可使用的物理网卡后退出",
    )
    default_uboot = Path(__file__).resolve().parents[2] / DEFAULT_UBOOT_IMAGE_NAME
    parser.add_argument(
        "--uboot-image",
        default=str(default_uboot),
        help="U-Boot 镜像路径；内容必须匹配项目锁定的文件哈希",
    )
    default_firmware = Path(__file__).resolve().parents[2] / DEFAULT_FACTORY_FIRMWARE_NAME
    parser.add_argument(
        "--firmware-image",
        default=str(default_firmware),
        help="Factory 固件路径；内容必须匹配项目锁定及上游发布的 SHA256",
    )
    parser.add_argument(
        "--firmware-backup",
        help="刷固件前验证的完整 split 备份目录；默认自动选择项目中最新备份",
    )
    parser.add_argument(
        "--uboot-web-url",
        default=DEFAULT_UBOOT_WEB_URL,
        help="已经进入恢复模式时的 U-Boot Web 地址",
    )
    parser.add_argument(
        "--firmware-reboot",
        action="store_true",
        help="固件刷写成功后由 U-Boot 自动重启；默认停留在 U-Boot Web",
    )
    parser.add_argument(
        "--firmware-timeout",
        type=positive_int,
        default=600,
        help="U-Boot 上传和刷写请求的最长等待秒数",
    )
    parser.add_argument("--output", help="本机输出目录")
    parser.add_argument(
        "--remote-target",
        help="split/刷写前备份的临时挂载点（必须位于 /mnt/；默认选择 U 盘或 p27）",
    )
    parser.add_argument(
        "--http-port",
        type=tcp_port,
        default=DEFAULT_HTTP_PORT,
        help="原厂 Web 传输不可用时，临时 BusyBox httpd 的起始端口",
    )
    parser.add_argument(
        "--pc-host",
        help="raw/刷 U-Boot 时供路由器连接的电脑局域网 IP；默认自动检测",
    )
    parser.add_argument(
        "--listen-host",
        default="0.0.0.0",
        help="raw/刷 U-Boot 时的本机监听地址",
    )
    parser.add_argument(
        "--stream-port",
        type=lambda value: tcp_port(value, allow_zero=True),
        default=0,
        help="raw/刷 U-Boot 时的本机端口；0 表示自动分配",
    )
    parser.add_argument(
        "--force-device",
        action="store_true",
        help="即使 APPSBL/ART 标签校验失败也继续（不会跳过 p1-p26 存在性检查）",
    )
    parser.add_argument("--rpc-timeout", type=positive_int, default=8, help=argparse.SUPPRESS)
    parser.add_argument("--telnet-wait", type=positive_int, default=20, help=argparse.SUPPRESS)
    parser.add_argument(
        "--command-timeout", type=positive_int, default=7200, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--raw-connect-timeout", type=positive_int, default=45, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--enter-timeout",
        type=positive_int,
        default=120,
        help="uBootEnter 等待路由器启动并回复的秒数",
    )
    parser.add_argument(
        "--uboot-http-timeout",
        type=positive_int,
        default=15,
        help="收到中断确认后等待 U-Boot Web 就绪的秒数",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="uBootEnter 成功后不自动打开默认浏览器",
    )
    parser.add_argument("--verbose", action="store_true", help="显示调试日志和异常堆栈")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def configure_logging(verbose: bool) -> None:
    """Configure one consistent console logger."""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def build_options(args: argparse.Namespace) -> BackupOptions:
    """Normalize parser output and prompt for the one required secret."""

    management_url, router_host = normalize_management_url(args.management_url)
    password = args.password
    if password is None:
        password = getpass.getpass("路由器后台/Telnet 密码: ")
    if not password:
        raise AthenaError("密码不能为空。")
    operation = "flash-uboot" if args.flash_uboot else "backup"
    if operation == "flash-uboot" and args.force_device:
        raise AthenaError("U-Boot 刷写不允许使用 --force-device 绕过设备校验。")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_output = (
        f"Athena_AX6600_uboot_flash_{timestamp}"
        if operation == "flash-uboot"
        else f"Athena_AX6600_backup_{timestamp}"
    )
    output = resolved_output(args.output, default_output)
    return BackupOptions(
        management_url=management_url,
        router_host=router_host,
        telnet_port=args.telnet_port,
        username=args.user,
        password=password,
        operation=operation,
        mode=args.mode,
        output=output,
        uboot_image=Path(args.uboot_image).expanduser().resolve(),
        remote_target=args.remote_target,
        http_port=args.http_port,
        pc_host=args.pc_host,
        listen_host=args.listen_host,
        stream_port=args.stream_port,
        force_device=args.force_device,
        rpc_timeout=float(args.rpc_timeout),
        telnet_wait=float(args.telnet_wait),
        command_timeout=float(args.command_timeout),
        raw_connect_timeout=float(args.raw_connect_timeout),
    )


def confirm_uboot_flash(plan: UbootFlashPlan) -> bool:
    """Present a final, hash-bound confirmation immediately before block writes."""

    LOGGER.warning("即将刷写 U-Boot；断电或写错设备可能导致路由器无法启动")
    LOGGER.warning("设备: %s", plan.model)
    LOGGER.warning("镜像: %s", plan.image.path)
    LOGGER.warning("SHA256: %s", plan.image.sha256)
    LOGGER.warning("写入顺序: %s", " -> ".join(plan.write_order))
    LOGGER.warning("刷写前备份: %s", ", ".join(plan.backup_files.values()))
    try:
        typed = input(f"请输入 {plan.confirmation_phrase} 以确认刷写: ").strip()
    except EOFError:
        return False
    return typed == plan.confirmation_phrase


def confirm_firmware_flash(plan: FirmwareFlashPlan) -> bool:
    """Show the second, remote-validated guard before U-Boot writes firmware."""

    LOGGER.warning("即将刷写 Factory 固件；现有系统 0 的内核/rootfs 将被覆盖")
    LOGGER.warning("镜像: %s", plan.image.path)
    LOGGER.warning("SHA256: %s", plan.image.sha256)
    LOGGER.warning("U-Boot: %s (%s)", plan.web_url, plan.uboot_version)
    LOGGER.warning("U-Boot 内存校验: %s", dict(plan.upload_info))
    LOGGER.warning("写入目标: %s", " -> ".join(plan.write_targets))
    LOGGER.warning("已验证恢复备份: %s", plan.backup.path)
    LOGGER.warning("GPT、U-Boot、ART 和系统 1 不会被写入")
    try:
        typed = input(f"请输入 {plan.confirmation_phrase} 以确认刷写: ").strip()
    except EOFError:
        return False
    return typed == plan.confirmation_phrase


def run_firmware_flash(args: argparse.Namespace) -> Path:
    """Reach U-Boot Web, validate twice, and execute the guarded firmware write."""

    if args.force_device:
        raise AthenaError("Factory 固件刷写不允许使用 --force-device 绕过任何校验。")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = resolved_output(args.output, f"Athena_AX6600_firmware_flash_{timestamp}")
    project_root = Path(__file__).resolve().parents[2]
    backup_dir = discover_backup(
        Path(args.firmware_backup) if args.firmware_backup else None,
        (project_root, Path.cwd()),
    )
    image_path = Path(args.firmware_image).expanduser().resolve()
    flasher = FirmwareFlasher(
        args.uboot_web_url,
        output,
        image_path,
        backup_dir,
        timeout=float(args.firmware_timeout),
    )
    version = flasher.probe_version(required=False)
    if version is None:
        LOGGER.info("U-Boot Web 尚未就绪，启动 uBootEnter")
        entered = UbootEnterService().run(
            args.flash_firmware,
            timeout=float(args.enter_timeout),
            http_timeout=float(args.uboot_http_timeout),
            open_browser=False,
        )
        flasher = FirmwareFlasher(
            entered.web_url,
            output,
            image_path,
            backup_dir,
            timeout=float(args.firmware_timeout),
        )
    return flasher.flash(confirm_firmware_flash, auto_reboot=bool(args.firmware_reboot))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""

    parser = create_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        if args.list_interfaces:
            print(format_interfaces(UbootEnterService().list_interfaces()))
            return 0
        if args.enter_uboot is not None:
            result = UbootEnterService().run(
                args.enter_uboot,
                timeout=float(args.enter_timeout),
                http_timeout=float(args.uboot_http_timeout),
                open_browser=not args.no_open_browser,
            )
            LOGGER.info(
                "uBootEnter 完成：%s，发送 %d 轮，耗时 %.1f 秒",
                result.web_url,
                result.attempts,
                result.elapsed_seconds,
            )
            return 0
        if args.flash_firmware is not None:
            report = run_firmware_flash(args)
            LOGGER.info("Factory 固件刷写完成；报告: %s", report)
            if not args.firmware_reboot:
                LOGGER.info("路由器仍停留在 U-Boot Web，请确认报告后手动重启")
            return 0
        options = build_options(args)
        LOGGER.info("管理地址: %s", options.management_url)
        LOGGER.info("操作: %s", options.operation)
        if options.operation == "backup":
            LOGGER.info("备份模式: %s", options.mode)
        LOGGER.info("输出目录: %s", options.output)
        artifacts = AthenaBackupRunner(options, confirm_uboot=confirm_uboot_flash).run()
    except KeyboardInterrupt:
        LOGGER.error("用户中止；已完成校验的文件会保留，路由器临时文件将被清理。")
        return 130
    except AthenaError as exc:
        LOGGER.error("%s", exc, exc_info=args.verbose)
        return 1
    except (OSError, ValueError) as exc:
        LOGGER.error("运行失败: %s", exc, exc_info=args.verbose)
        return 1
    except Exception as exc:  # defensive CLI boundary; --verbose retains diagnostics
        LOGGER.error("发生未预期错误: %s", exc, exc_info=args.verbose)
        return 1
    if options.operation == "flash-uboot":
        LOGGER.info("U-Boot 刷写完成；刷写前备份文件数: %d", len(artifacts))
        LOGGER.info("刷写报告: %s", Path(options.output) / "uboot-flash-report.json")
    else:
        LOGGER.info("备份完成：%d 个镜像文件", len(artifacts))
        LOGGER.info("校验清单: %s", Path(options.output) / "SHA256SUMS")
    LOGGER.info("本工具不包含自动恢复功能。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
