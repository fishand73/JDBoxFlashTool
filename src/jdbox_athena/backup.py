"""High-level, read-source-only Athena backup workflow."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .constants import FIRST_PARTITION, LAST_PARTITION
from .device import DeviceInspector
from .errors import AthenaError, RpcError
from .flash import ConfirmationCallback, UbootFlasher
from .integrity import write_manifests
from .jdcapi import JdcApiClient
from .models import Artifact, Partition
from .telnet_client import MiniTelnet
from .transfer import RawStreamer, RemoteWorkspace, partition_filename
from .util import is_tcp_open

LOGGER = logging.getLogger(__name__)
TelnetRecoveryCallback = Callable[[RpcError], None]


@dataclass(frozen=True)
class BackupOptions:
    """Validated CLI options consumed by the workflow."""

    management_url: str
    router_host: str
    telnet_port: int
    username: str
    password: str
    operation: str
    mode: str
    output: Path
    uboot_image: Optional[Path]
    remote_target: Optional[str]
    http_port: int
    pc_host: Optional[str]
    listen_host: str
    stream_port: int
    force_device: bool
    rpc_timeout: float
    telnet_wait: float
    command_timeout: float
    raw_connect_timeout: float


class AthenaBackupRunner:
    """Coordinate Telnet enablement, validation, backup, transfer, and checksums."""

    def __init__(
        self,
        options: BackupOptions,
        confirm_uboot: Optional[ConfirmationCallback] = None,
        recover_telnet: Optional[TelnetRecoveryCallback] = None,
    ) -> None:
        self.options = options
        self.confirm_uboot = confirm_uboot
        self.recover_telnet = recover_telnet
        self.shell: Optional[MiniTelnet] = None
        self.workspace: Optional[RemoteWorkspace] = None
        self.artifacts: List[Artifact] = []
        self.telnet_strategy = "already-open"
        self.started_at = datetime.now(timezone.utc)

    def _write_run_info(self, status: str, error: Optional[str] = None) -> None:
        payload: Dict[str, object] = {
            "schema_version": 1,
            "status": status,
            "operation": self.options.operation,
            "mode": self.options.mode,
            "management_url": self.options.management_url,
            "router_host": self.options.router_host,
            "telnet_strategy": self.telnet_strategy,
            "started_at": self.started_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "automatic_restore_available": False,
            "artifact_count": len(self.artifacts),
        }
        if error:
            payload["error"] = error
        (self.options.output / "backup-info.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _record(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)
        # Keep already-verified files useful if a later partition fails.
        write_manifests(self.options.output, self.artifacts)

    def ensure_telnet(self) -> None:
        """Open Telnet only when needed, validating the actual TCP endpoint."""

        host = self.options.router_host
        port = self.options.telnet_port
        LOGGER.info("检测 Telnet %s:%d", host, port)
        if is_tcp_open(host, port):
            LOGGER.info("Telnet 已开放")
            return
        LOGGER.info("Telnet 未开放，使用后台密码调用 /jdcapi session/login")
        client = JdcApiClient(self.options.management_url, timeout=self.options.rpc_timeout)
        client.login(self.options.username, self.options.password)
        try:
            self.telnet_strategy = client.enable_telnet(
                host,
                port,
                wait_seconds=self.options.telnet_wait,
            )
        except RpcError as exc:
            if self.recover_telnet is None:
                raise
            LOGGER.warning("两种 Telnet 开启策略均失败：%s", exc)
            self.recover_telnet(exc)
            LOGGER.info("原厂固件恢复完成，重新登录并开启 Telnet")
            client = JdcApiClient(self.options.management_url, timeout=self.options.rpc_timeout)
            client.login(self.options.username, self.options.password)
            self.telnet_strategy = "r4211-recovery+" + client.enable_telnet(
                host,
                port,
                wait_seconds=self.options.telnet_wait,
            )
        LOGGER.info("Telnet 已开放，生效策略: %s", self.telnet_strategy)

    def connect_shell(self) -> MiniTelnet:
        """Create and verify a root Telnet shell."""

        self.shell = MiniTelnet(
            self.options.router_host,
            self.options.telnet_port,
            timeout=10.0,
        )
        self.shell.login(self.options.username, self.options.password)
        LOGGER.info("Telnet root shell 登录成功")
        return self.shell

    def _backup_staged(
        self,
        source: str,
        filename: str,
        kind: str,
        *,
        block_size: str = "4M",
        skip: Optional[int] = None,
        count: Optional[int] = None,
        partition: Optional[Partition] = None,
    ) -> Artifact:
        if self.workspace is None:
            raise AthenaError("分区传输工作区未初始化。")
        LOGGER.info("备份 %s -> %s", source, filename)
        try:
            size, remote_md5, remote_sha256 = self.workspace.stage_block_device(
                source,
                filename,
                block_size=block_size,
                skip=skip,
                count=count,
            )
            _path, local_md5, local_sha256 = self.workspace.download_verified(
                filename,
                size,
                remote_md5,
                remote_sha256,
            )
        finally:
            self.workspace.remove_staged(filename)
        artifact = Artifact(
            filename=filename,
            source=source,
            size_bytes=size,
            md5=local_md5,
            sha256=local_sha256,
            kind=kind,
            partition_number=partition.number if partition else None,
            partition_label=partition.label if partition else None,
        )
        self._record(artifact)
        return artifact

    def backup_split(
        self,
        partitions: Dict[int, Partition],
        total_sectors: int,
        sector_size: int,
    ) -> None:
        """Back up both GPT headers/tables and p1 through p26."""

        if self.shell is None:
            raise AthenaError("Telnet shell 未连接。")
        selected = [partitions[number] for number in range(FIRST_PARTITION, LAST_PARTITION + 1)]
        self.workspace = RemoteWorkspace(
            self.shell,
            self.options.router_host,
            self.options.output,
            remote_target=self.options.remote_target,
            http_port=self.options.http_port,
            command_timeout=self.options.command_timeout,
        )
        self.workspace.prepare(selected)
        self.workspace.setup_http()
        self._backup_staged(
            "/dev/mmcblk0",
            "gpt-primary.bin",
            "gpt-primary",
            block_size=str(sector_size),
            count=34,
        )
        self._backup_staged(
            "/dev/mmcblk0",
            "gpt-backup.bin",
            "gpt-backup",
            block_size=str(sector_size),
            skip=total_sectors - 33,
            count=33,
        )
        for partition in selected:
            self._backup_staged(
                f"/dev/{partition.name}",
                partition_filename(partition),
                "partition",
                partition=partition,
            )

    def backup_raw(self) -> None:
        """Safely stream the fixed raw prefix and verify source against destination."""

        if self.shell is None:
            raise AthenaError("Telnet shell 未连接。")
        streamer = RawStreamer(
            self.shell,
            self.options.router_host,
            self.options.output,
            pc_host=self.options.pc_host,
            listen_host=self.options.listen_host,
            listen_port=self.options.stream_port,
            connect_timeout=self.options.raw_connect_timeout,
            command_timeout=self.options.command_timeout,
        )
        self._record(streamer.stream())

    def flash_uboot(
        self,
        partitions: Dict[int, Partition],
        model: str,
        release: str,
    ) -> None:
        """Create fresh APPSBL backups, then run the guarded dual-copy flasher."""

        if self.shell is None:
            raise AthenaError("Telnet shell 未连接。")
        if self.options.uboot_image is None:
            raise AthenaError("未指定 U-Boot 镜像。")
        if self.confirm_uboot is None:
            raise AthenaError("U-Boot 刷写缺少交互确认处理器。")
        selected = [partitions[13], partitions[14]]
        self.workspace = RemoteWorkspace(
            self.shell,
            self.options.router_host,
            self.options.output,
            remote_target=self.options.remote_target,
            http_port=self.options.http_port,
            command_timeout=self.options.command_timeout,
        )
        self.workspace.prepare(selected)
        self.workspace.setup_http()
        backup_files: Dict[str, str] = {}
        for partition in selected:
            filename = (
                f"preflash_p{partition.number:02d}_"
                f"{(partition.label or 'unlabeled').replace(':', '_')}.bin"
            )
            artifact = self._backup_staged(
                f"/dev/{partition.name}",
                filename,
                "preflash-uboot-backup",
                partition=partition,
            )
            backup_files[f"/dev/{partition.name}"] = artifact.filename
        self.workspace.cleanup()
        self.workspace = None
        flasher = UbootFlasher(
            self.shell,
            self.options.router_host,
            self.options.output,
            self.options.uboot_image,
            pc_host=self.options.pc_host,
            listen_host=self.options.listen_host,
            listen_port=self.options.stream_port,
            command_timeout=self.options.command_timeout,
        )
        report = flasher.flash(
            partitions,
            model,
            release,
            backup_files,
            self.confirm_uboot,
        )
        LOGGER.info("U-Boot 双分区写入与回读校验完成: %s", report)
        LOGGER.warning("工具不会自动重启；请确认报告为 complete 后再手动重启或进入 failsafe")

    def run(self) -> List[Artifact]:
        """Execute the selected workflow and always clean generated router state."""

        self.options.output.mkdir(parents=True, exist_ok=True)
        self._write_run_info("running")
        try:
            self.ensure_telnet()
            shell = self.connect_shell()
            inspector = DeviceInspector(shell)
            info = inspector.collect(self.options.output / "device-info.txt")
            model = info.get("model", "").strip()
            if model:
                LOGGER.info("设备型号: %s", model)
            partitions = inspector.read_partitions()
            force_device = (
                self.options.force_device if self.options.operation == "backup" else False
            )
            inspector.verify(partitions, force=force_device)
            if force_device:
                LOGGER.warning("--force-device 已启用：已绕过 APPSBL/ART 布局保护")
            total_sectors, sector_size = inspector.disk_geometry()
            LOGGER.info(
                "已确认 p1-p26；mmcblk0 共 %d 个逻辑扇区，每扇区 %d bytes",
                total_sectors,
                sector_size,
            )
            if self.options.operation == "flash-uboot":
                self.flash_uboot(
                    partitions,
                    info.get("model", "").strip(),
                    info.get("openwrt_release", "").strip(),
                )
                write_manifests(self.options.output, self.artifacts)
                self._write_run_info("complete")
                return list(self.artifacts)
            if self.options.mode in {"split", "both"}:
                self.backup_split(partitions, total_sectors, sector_size)
                if self.workspace:
                    self.workspace.cleanup()
                    self.workspace = None
            if self.options.mode in {"raw", "both"}:
                self.backup_raw()
            write_manifests(self.options.output, self.artifacts)
            self._write_run_info("complete")
            return list(self.artifacts)
        except Exception as exc:
            self._write_run_info("incomplete", error=str(exc))
            raise
        finally:
            if self.workspace:
                self.workspace.cleanup()
            if self.shell:
                self.shell.close()
