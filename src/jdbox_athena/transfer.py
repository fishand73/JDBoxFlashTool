"""Temporary router workspace, HTTP downloads, and safe raw streaming."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple

from .constants import RAW_PREFIX_BYTES, RAW_PREFIX_MIB
from .errors import IntegrityError, TransferError
from .models import Artifact, Partition
from .telnet_client import MiniTelnet
from .util import discover_local_ip, human_size, safe_filename, shell_quote

LOGGER = logging.getLogger(__name__)
DOWNLOAD_CHUNK = 4 * 1024 * 1024


class RemoteWorkspace:
    """Stage one split-backup artifact at a time and serve it over HTTP."""

    def __init__(
        self,
        shell: MiniTelnet,
        router_host: str,
        output: Path,
        remote_target: Optional[str] = None,
        http_port: int = 18080,
        command_timeout: float = 7200.0,
    ) -> None:
        self.shell = shell
        self.router_host = router_host
        self.output = output
        self.requested_target = remote_target
        self.preferred_http_port = http_port
        self.command_timeout = command_timeout
        self.token = secrets.token_hex(6)
        self.target: Optional[str] = None
        self.remote_dir: Optional[str] = None
        self.base_url: Optional[str] = None
        self.web_alias: Optional[str] = None
        self.http_pid: Optional[str] = None

    def prepare(self, partitions: Iterable[Partition]) -> None:
        """Choose writable temporary storage large enough for the largest partition."""

        partition_list = list(partitions)
        if not partition_list:
            raise TransferError("没有可备份的分区。")
        if self.requested_target:
            candidates = [self.requested_target.rstrip("/")]
        else:
            mounts = self.shell.run(
                "awk '$2 ~ /^\\/mnt\\// {print $2}' /proc/mounts", check=False
            )
            candidates = [line.strip().rstrip("/") for line in mounts.splitlines() if line.strip()]

            def priority(path: str) -> Tuple[int, str]:
                # Prefer removable media, while keeping p27 as an automatic fallback.
                if re.search(r"/mnt/(sd[a-z]|usb)", path, flags=re.IGNORECASE):
                    return (0, path)
                if path == "/mnt/mmcblk0p27":
                    return (1, path)
                return (2, path)

            candidates = sorted(dict.fromkeys(candidates), key=priority)
        candidates = [candidate for candidate in candidates if candidate.startswith("/mnt/")]
        if not candidates:
            raise TransferError(
                "没有找到可用的 /mnt 临时存储；请挂载 U 盘或使用 --remote-target /mnt/...。"
            )
        required = max(partition.size_bytes for partition in partition_list) + 64 * 1024 * 1024
        for candidate in candidates:
            probe = f"{candidate}/.athena_probe_{self.token}"
            _output, status = self.shell.exec(
                f"touch {shell_quote(probe)} 2>/dev/null && rm -f {shell_quote(probe)}"
            )
            if status != 0:
                continue
            free_text = self.shell.run(
                f"df -Pk {shell_quote(candidate)} 2>/dev/null | awk 'NR==2 {{print $4}}'",
                check=False,
            ).strip()
            try:
                free_bytes = int(free_text.splitlines()[-1]) * 1024
            except (ValueError, IndexError):
                continue
            if free_bytes < required:
                LOGGER.warning(
                    "%s 空间不足：可用 %s，需要约 %s",
                    candidate,
                    human_size(free_bytes),
                    human_size(required),
                )
                continue
            self.target = candidate
            self.remote_dir = f"{candidate}/.athena_backup_tmp_{self.token}"
            self.shell.run(f"mkdir -p {shell_quote(self.remote_dir)}")
            LOGGER.info("临时存储: %s（可用 %s）", candidate, human_size(free_bytes))
            return
        raise TransferError(
            f"已找到挂载点，但没有可写且至少剩余 {human_size(required)} 的临时存储。"
        )

    def _require_remote_dir(self) -> str:
        if self.remote_dir is None:
            raise TransferError("远程临时目录尚未初始化。")
        return self.remote_dir

    def _probe_url(self, base_url: str) -> bool:
        remote_dir = self._require_remote_dir()
        filename = f"probe_{self.token}.txt"
        expected = f"ATHENA_HTTP_{self.token}"
        remote_path = f"{remote_dir}/{filename}"
        self.shell.run(f"printf %s {shell_quote(expected)} > {shell_quote(remote_path)}")
        try:
            with urllib.request.urlopen(base_url + filename, timeout=5.0) as response:
                return response.read(1024).decode("utf-8", errors="replace") == expected
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
        finally:
            self.shell.run(f"rm -f {shell_quote(remote_path)}", check=False)

    def setup_http(self) -> None:
        """Expose the random temporary directory, preferring the existing web server."""

        remote_dir = self._require_remote_dir()
        alias_name = f".athena_backup_{self.token}"
        alias_path = f"/www/{alias_name}"
        _output, status = self.shell.exec(
            f"rm -f {shell_quote(alias_path)}; "
            f"ln -s {shell_quote(remote_dir)} {shell_quote(alias_path)}"
        )
        if status == 0:
            base_url = f"http://{self.router_host}/{alias_name}/"
            if self._probe_url(base_url):
                self.web_alias = alias_path
                self.base_url = base_url
                LOGGER.info("使用路由器原厂 Web 服务传输分区备份")
                return
            self.shell.run(f"rm -f {shell_quote(alias_path)}", check=False)
        applets = self.shell.run("busybox --list 2>/dev/null", check=False).splitlines()
        if "httpd" not in applets:
            raise TransferError("原厂 Web 服务无法读取临时目录，BusyBox 也没有 httpd。")
        for port in range(self.preferred_http_port, self.preferred_http_port + 5):
            pid = self.shell.run(
                f"busybox httpd -f -p {port} -h {shell_quote(remote_dir)} "
                ">/dev/null 2>&1 & echo $!",
                check=False,
            ).strip()
            if not pid:
                continue
            process_id = pid.splitlines()[-1]
            base_url = f"http://{self.router_host}:{port}/"
            time.sleep(0.5)
            if self._probe_url(base_url):
                self.http_pid = process_id
                self.base_url = base_url
                LOGGER.info("使用临时 BusyBox HTTP 端口 %d 传输分区备份", port)
                return
            self.shell.run(f"kill {shell_quote(process_id)} 2>/dev/null || true", check=False)
        raise TransferError("无法建立路由器到电脑的 HTTP 下载通道。")

    def stage_block_device(
        self,
        source: str,
        filename: str,
        *,
        block_size: str = "4M",
        skip: Optional[int] = None,
        count: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        """Copy a fixed block-device range to temporary storage and hash it remotely."""

        remote_dir = self._require_remote_dir()
        remote_path = f"{remote_dir}/{filename}"
        options = [
            f"if={shell_quote(source)}",
            f"of={shell_quote(remote_path)}",
            f"bs={shell_quote(block_size)}",
        ]
        if skip is not None:
            options.append(f"skip={skip}")
        if count is not None:
            options.append(f"count={count}")
        command = (
            f"rm -f {shell_quote(remote_path)}; dd {' '.join(options)} && sync"
        )
        self.shell.run(command, timeout=self.command_timeout)
        metadata = self.shell.run(
            f"wc -c < {shell_quote(remote_path)}; "
            f"busybox md5sum {shell_quote(remote_path)}; "
            f"busybox sha256sum {shell_quote(remote_path)}",
            timeout=self.command_timeout,
        )
        size_match = re.search(r"(?m)^\s*(\d+)\s*$", metadata)
        md5_match = re.search(r"(?mi)^([0-9a-f]{32})\s+", metadata)
        sha_match = re.search(r"(?mi)^([0-9a-f]{64})\s+", metadata)
        if not size_match or not md5_match or not sha_match:
            raise TransferError(f"无法解析远程文件校验信息: {metadata}")
        return int(size_match.group(1)), md5_match.group(1).lower(), sha_match.group(1).lower()

    def download_verified(
        self,
        filename: str,
        expected_size: int,
        expected_md5: str,
        expected_sha256: str,
    ) -> Tuple[Path, str, str]:
        """Download one file and require exact size, MD5, and SHA-256 matches."""

        if self.base_url is None:
            raise TransferError("HTTP 下载通道尚未初始化。")
        destination = self.output / filename
        temporary = destination.with_suffix(destination.suffix + ".part")
        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        downloaded = 0
        next_progress = 10
        request = urllib.request.Request(
            self.base_url + filename,
            headers={"User-Agent": "JDBox-Athena-Backup/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response, temporary.open(
                "wb"
            ) as handle:
                while True:
                    block = response.read(DOWNLOAD_CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    md5.update(block)
                    sha256.update(block)
                    downloaded += len(block)
                    percent = int(downloaded * 100 / expected_size) if expected_size else 100
                    if percent >= next_progress:
                        LOGGER.info(
                            "  %s: %d%% (%s)",
                            filename,
                            min(percent, 100),
                            human_size(downloaded),
                        )
                        next_progress += 10
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        actual_md5 = md5.hexdigest()
        actual_sha256 = sha256.hexdigest()
        problems = []
        if downloaded != expected_size:
            problems.append(f"size {downloaded} != {expected_size}")
        if actual_md5 != expected_md5:
            problems.append(f"MD5 {actual_md5} != {expected_md5}")
        if actual_sha256 != expected_sha256:
            problems.append(f"SHA256 {actual_sha256} != {expected_sha256}")
        if problems:
            temporary.unlink(missing_ok=True)
            raise IntegrityError(f"{filename} 校验失败: " + "; ".join(problems))
        os.replace(temporary, destination)
        return destination, actual_md5, actual_sha256

    def remove_staged(self, filename: str) -> None:
        """Delete one generated temporary file after verified download."""

        remote_path = f"{self._require_remote_dir()}/{filename}"
        self.shell.run(f"rm -f {shell_quote(remote_path)}", check=False)

    def cleanup(self) -> None:
        """Remove only paths and processes created with this instance's random token."""

        if self.web_alias:
            self.shell.run(f"rm -f {shell_quote(self.web_alias)}", check=False)
            self.web_alias = None
        if self.http_pid:
            self.shell.run(
                f"kill {shell_quote(self.http_pid)} 2>/dev/null || true", check=False
            )
            self.http_pid = None
        if self.remote_dir:
            self.shell.run(f"rm -rf {shell_quote(self.remote_dir)}", check=False)
            self.remote_dir = None


class RawStreamer:
    """Stream the first 2555 MiB directly to the PC without staging on eMMC."""

    def __init__(
        self,
        shell: MiniTelnet,
        router_host: str,
        output: Path,
        *,
        pc_host: Optional[str] = None,
        listen_host: str = "0.0.0.0",
        listen_port: int = 0,
        connect_timeout: float = 45.0,
        idle_timeout: float = 120.0,
        command_timeout: float = 7200.0,
    ) -> None:
        self.shell = shell
        self.router_host = router_host
        self.output = output
        self.pc_host = pc_host
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.command_timeout = command_timeout

    def _router_addresses(self) -> Set[str]:
        addresses: Set[str] = {self.router_host}
        try:
            for item in socket.getaddrinfo(self.router_host, None, type=socket.SOCK_STREAM):
                addresses.add(str(item[4][0]))
        except OSError:
            pass
        return addresses

    def _remote_hash(self, algorithm: str) -> str:
        output = self.shell.run(
            f"dd if=/dev/mmcblk0 bs=1M count={RAW_PREFIX_MIB} 2>/dev/null "
            f"| busybox {algorithm}sum",
            timeout=self.command_timeout,
        )
        width = 32 if algorithm == "md5" else 64
        match = re.search(rf"(?i)([0-9a-f]{{{width}}})", output)
        if not match:
            raise TransferError(f"无法解析路由器端 {algorithm.upper()}：{output}")
        return match.group(1).lower()

    def stream(self) -> Artifact:
        """Receive, hash, re-hash at source, and finalize the fixed 2555 MiB image."""

        applets = set(self.shell.run("busybox --list 2>/dev/null", check=False).splitlines())
        missing = {"nc", "md5sum", "sha256sum"}.difference(applets)
        if missing:
            raise TransferError(
                "BusyBox 缺少 raw 流式备份所需 applet: " + ", ".join(sorted(missing))
            )
        destination = self.output / f"mmcblk0-prefix-{RAW_PREFIX_MIB}MiB.img"
        temporary = destination.with_suffix(destination.suffix + ".part")
        local_address = self.pc_host or discover_local_ip(self.router_host)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.listen_host, self.listen_port))
            listener.listen(2)
        except OSError as exc:
            listener.close()
            raise TransferError(f"无法打开本机 raw 接收端口: {exc}") from exc
        actual_port = int(listener.getsockname()[1])
        listener.settimeout(1.0)
        LOGGER.info(
            "raw 安全策略：直接流式传到 %s:%d，不在路由器 p27 暂存 2555 MiB",
            local_address,
            actual_port,
        )
        token = secrets.token_hex(5)
        dd_log = f"/tmp/.athena_raw_{token}.log"
        remote_command = (
            f"dd if=/dev/mmcblk0 bs=1M count={RAW_PREFIX_MIB} 2>{shell_quote(dd_log)} "
            f"| busybox nc {shell_quote(local_address)} {actual_port}; "
            "athena_pipe_rc=$?; "
            f"cat {shell_quote(dd_log)}; rm -f {shell_quote(dd_log)}; "
            'test "$athena_pipe_rc" -eq 0'
        )
        remote_result: Dict[str, object] = {}

        def run_remote() -> None:
            try:
                remote_result["value"] = self.shell.exec(
                    remote_command, timeout=self.command_timeout
                )
            except BaseException as exc:  # handed back to the main thread
                remote_result["error"] = exc

        worker = threading.Thread(target=run_remote, name="athena-raw-sender", daemon=True)
        worker.start()
        connection: Optional[socket.socket] = None
        deadline = time.monotonic() + self.connect_timeout
        allowed_peers = self._router_addresses()
        try:
            while time.monotonic() < deadline:
                try:
                    candidate, peer = listener.accept()
                except socket.timeout:
                    continue
                if str(peer[0]) not in allowed_peers:
                    candidate.close()
                    continue
                connection = candidate
                break
            if connection is None:
                raise TransferError(
                    "路由器未能连接本机 raw 接收端口；请检查 Windows 防火墙，"
                    "必要时用 --pc-host 指定电脑局域网 IP。"
                )
            connection.settimeout(self.idle_timeout)
            md5 = hashlib.md5()
            sha256 = hashlib.sha256()
            received = 0
            next_progress = 5
            with connection, temporary.open("wb") as handle:
                while True:
                    block = connection.recv(DOWNLOAD_CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    md5.update(block)
                    sha256.update(block)
                    received += len(block)
                    percent = int(received * 100 / RAW_PREFIX_BYTES)
                    if percent >= next_progress:
                        LOGGER.info("  raw: %d%% (%s)", min(percent, 100), human_size(received))
                        next_progress += 5
            worker.join(timeout=30.0)
            if worker.is_alive():
                raise TransferError("raw 数据已接收，但路由器端命令没有正常结束。")
            if "error" in remote_result:
                error = remote_result["error"]
                if isinstance(error, BaseException):
                    raise error
            result = remote_result.get("value")
            if not isinstance(result, tuple) or len(result) != 2 or result[1] != 0:
                raise TransferError(f"路由器端 raw 命令失败: {result}")
            if received != RAW_PREFIX_BYTES:
                raise IntegrityError(
                    f"raw 大小不完整: {received} != {RAW_PREFIX_BYTES} bytes"
                )
            local_md5 = md5.hexdigest()
            local_sha256 = sha256.hexdigest()
            LOGGER.info("重新读取源数据计算远端 MD5/SHA256（不会写入 eMMC）")
            remote_md5 = self._remote_hash("md5")
            remote_sha256 = self._remote_hash("sha256")
            if local_md5 != remote_md5 or local_sha256 != remote_sha256:
                raise IntegrityError(
                    "raw 源/目标校验失败: "
                    f"MD5 {local_md5} != {remote_md5} 或 "
                    f"SHA256 {local_sha256} != {remote_sha256}"
                )
            os.replace(temporary, destination)
            return Artifact(
                filename=destination.name,
                source=f"/dev/mmcblk0 first {RAW_PREFIX_MIB} MiB",
                size_bytes=received,
                md5=local_md5,
                sha256=local_sha256,
                kind="raw-prefix",
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            listener.close()


def partition_filename(partition: Partition) -> str:
    """Return a stable, sortable filename for a split partition image."""

    label = safe_filename(partition.label or "unlabeled")
    return f"p{partition.number:02d}_{label}.bin"
