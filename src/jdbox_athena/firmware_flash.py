"""Guarded OpenWrt factory flashing through the compatible U-Boot Web API."""

from __future__ import annotations

import http.client
import json
import logging
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from .constants import (
    DEFAULT_FACTORY_FIRMWARE_MD5,
    DEFAULT_FACTORY_FIRMWARE_SHA256,
    DEFAULT_FACTORY_FIRMWARE_SIZE,
    DEFAULT_FACTORY_KERNEL_SIZE,
)
from .errors import FirmwareFlashError
from .integrity import hash_file
from .util import human_size

LOGGER = logging.getLogger(__name__)
HTTP_RESPONSE_LIMIT = 64 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024
FIT_MAGIC = b"\xd0\x0d\xfe\xed"
SQUASHFS_MAGIC = b"hsqs"
REQUIRED_PARTITION_LABELS = {
    2: "0:BOOTCONFIG",
    3: "0:BOOTCONFIG1",
    13: "0:APPSBL",
    14: "0:APPSBL_1",
    15: "0:ART",
    16: "0:HLOS",
    17: "0:HLOS_1",
    18: "rootfs",
    19: "0:WIFIFW",
    20: "rootfs_1",
    21: "0:WIFIFW_1",
    22: "rootfs_data",
}


@dataclass(frozen=True)
class FactoryFirmwareInfo:
    """Metadata locked to the requested RE-CS-02 factory image."""

    path: str
    filename: str
    size_bytes: int
    kernel_size_bytes: int
    rootfs_size_bytes: int
    md5: str
    sha256: str
    format: str
    device: str


@dataclass(frozen=True)
class FirmwareBackupInfo:
    """Verified recovery material required before a firmware write."""

    path: str
    verified_files: Tuple[str, ...]
    schema_version: int


@dataclass(frozen=True)
class FirmwareFlashPlan:
    """All validated details shown immediately before the destructive request."""

    image: FactoryFirmwareInfo
    backup: FirmwareBackupInfo
    web_url: str
    uboot_version: str
    upload_info: Mapping[str, Any]
    write_targets: Tuple[str, ...]
    confirmation_phrase: str
    auto_reboot: bool


ConfirmationCallback = Callable[[FirmwareFlashPlan], bool]


def normalize_uboot_web_url(value: str) -> str:
    """Accept only a root-level plain HTTP U-Boot Web address."""

    candidate = value.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme != "http" or not parsed.hostname:
        raise FirmwareFlashError("U-Boot Web 地址必须是有效的 http:// 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FirmwareFlashError("U-Boot Web 地址不能包含凭据、查询参数或片段。")
    return urlunparse(("http", parsed.netloc, "/", "", "", ""))


def discover_backup(explicit: Optional[Path], roots: Iterable[Path]) -> Path:
    """Find the newest complete split backup unless one was specified."""

    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        if not candidate.is_dir():
            raise FirmwareFlashError(f"找不到刷写前备份目录: {candidate}")
        return candidate
    candidates: List[Path] = []
    seen = set()
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        candidates.extend(
            path
            for path in resolved.glob("Athena_AX6600_backup_*")
            if path.is_dir() and (path / "manifest.json").is_file()
        )
    if not candidates:
        raise FirmwareFlashError(
            "没有找到完整的 GPT+p1-p26 备份；请先运行默认 split 备份，"
            "或使用 --firmware-backup 指定已有备份目录。"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


class FirmwareFlasher:
    """Validate locally, validate again in U-Boot RAM, then commit exactly once."""

    def __init__(
        self,
        web_url: str,
        output: Path,
        image_path: Path,
        backup_dir: Path,
        *,
        timeout: float = 600.0,
    ) -> None:
        self.web_url = normalize_uboot_web_url(web_url)
        self.output = output
        self.image_path = image_path
        self.backup_dir = backup_dir
        self.timeout = timeout
        self.report_path = output / "firmware-flash-report.json"
        self.report: Dict[str, Any] = {
            "schema_version": 1,
            "operation": "flash-firmware",
            "status": "created",
        }

    def _write_report(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def validate_image(self) -> FactoryFirmwareInfo:
        """Require the exact official asset plus its FIT/rootfs structure."""

        path = self.image_path.expanduser().resolve()
        if not path.is_file():
            raise FirmwareFlashError(f"找不到 Factory 固件: {path}")
        size = path.stat().st_size
        if size != DEFAULT_FACTORY_FIRMWARE_SIZE:
            raise FirmwareFlashError(
                f"Factory 固件大小不符: {size} != {DEFAULT_FACTORY_FIRMWARE_SIZE} bytes"
            )
        with path.open("rb") as handle:
            kernel = handle.read(DEFAULT_FACTORY_KERNEL_SIZE)
            rootfs_magic = handle.read(len(SQUASHFS_MAGIC))
        if len(kernel) != DEFAULT_FACTORY_KERNEL_SIZE or kernel[:4] != FIT_MAGIC:
            raise FirmwareFlashError("Factory 固件开头不是预期的 ARM64 OpenWrt FIT。")
        fit_size = int.from_bytes(kernel[4:8], "big")
        if fit_size < 4096 or fit_size > DEFAULT_FACTORY_KERNEL_SIZE:
            raise FirmwareFlashError(f"FIT 声明大小异常: {fit_size} bytes")
        if rootfs_magic != SQUASHFS_MAGIC:
            raise FirmwareFlashError(
                f"未在 {DEFAULT_FACTORY_KERNEL_SIZE // 1024 // 1024} MiB 偏移找到 SquashFS。"
            )
        if b"jdcloud,re-cs-02" not in kernel or b"ARM64 OpenWrt" not in kernel:
            raise FirmwareFlashError("FIT 中未找到 JDCloud RE-CS-02 / ARM64 OpenWrt 标识。")
        md5, sha256 = hash_file(path)
        if md5.lower() != DEFAULT_FACTORY_FIRMWARE_MD5:
            raise FirmwareFlashError(f"Factory 固件 MD5 不匹配: {md5}")
        if sha256.lower() != DEFAULT_FACTORY_FIRMWARE_SHA256:
            raise FirmwareFlashError(f"Factory 固件 SHA256 不匹配: {sha256}")
        return FactoryFirmwareInfo(
            path=str(path),
            filename=path.name,
            size_bytes=size,
            kernel_size_bytes=DEFAULT_FACTORY_KERNEL_SIZE,
            rootfs_size_bytes=size - DEFAULT_FACTORY_KERNEL_SIZE,
            md5=md5.lower(),
            sha256=sha256.lower(),
            format="ARM64 OpenWrt FIT + SquashFS Factory",
            device="jdcloud_re-cs-02",
        )

    @staticmethod
    def _load_sha256s(path: Path) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            values[parts[1].lstrip("*")] = parts[0].lower()
        return values

    def validate_backup(self) -> FirmwareBackupInfo:
        """Verify recovery-critical files against the tool-generated manifest."""

        backup = self.backup_dir.expanduser().resolve()
        manifest_path = backup / "manifest.json"
        sums_path = backup / "SHA256SUMS"
        if not manifest_path.is_file() or not sums_path.is_file():
            raise FirmwareFlashError("备份缺少 manifest.json 或 SHA256SUMS。")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FirmwareFlashError(f"无法读取备份 manifest: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("read_only_backup") is not True:
            raise FirmwareFlashError("备份 manifest 不是本工具生成的只读备份格式。")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            raise FirmwareFlashError("备份 manifest 缺少 artifacts。")
        by_partition: Dict[int, Mapping[str, Any]] = {}
        by_kind: Dict[str, Mapping[str, Any]] = {}
        for value in artifacts:
            if not isinstance(value, dict):
                continue
            number = value.get("partition_number")
            kind = value.get("kind")
            if isinstance(number, int):
                by_partition[number] = value
            if isinstance(kind, str):
                by_kind[kind] = value
        required = []
        for kind in ("gpt-primary", "gpt-backup"):
            artifact = by_kind.get(kind)
            if artifact is None:
                raise FirmwareFlashError(f"备份缺少 {kind}。")
            required.append(artifact)
        for number, expected_label in REQUIRED_PARTITION_LABELS.items():
            artifact = by_partition.get(number)
            if artifact is None:
                raise FirmwareFlashError(f"备份缺少关键分区 p{number}。")
            if artifact.get("partition_label") != expected_label:
                raise FirmwareFlashError(
                    f"备份 p{number} 标签不符: {artifact.get('partition_label')!r} "
                    f"!= {expected_label!r}"
                )
            required.append(artifact)
        sums = self._load_sha256s(sums_path)
        verified = []
        LOGGER.info("校验刷写前恢复材料: %s", backup)
        for artifact in required:
            filename = artifact.get("filename")
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise FirmwareFlashError("备份 manifest 包含无效文件名。")
            path = backup / filename
            if not path.is_file():
                raise FirmwareFlashError(f"备份文件缺失: {filename}")
            expected_size = artifact.get("size_bytes")
            if not isinstance(expected_size, int) or path.stat().st_size != expected_size:
                raise FirmwareFlashError(f"备份文件大小不符: {filename}")
            expected_sha = artifact.get("sha256")
            if not isinstance(expected_sha, str) or sums.get(filename) != expected_sha.lower():
                raise FirmwareFlashError(f"备份 SHA256 清单不一致: {filename}")
            _md5, actual_sha = hash_file(path)
            if actual_sha.lower() != expected_sha.lower():
                raise FirmwareFlashError(f"备份文件已损坏: {filename}")
            verified.append(filename)
        schema = manifest.get("schema_version")
        return FirmwareBackupInfo(
            path=str(backup),
            verified_files=tuple(verified),
            schema_version=schema if isinstance(schema, int) else 0,
        )

    def _connection(self) -> http.client.HTTPConnection:
        parsed = urlparse(self.web_url)
        host = parsed.hostname
        if host is None:  # normalize_uboot_web_url already enforces this
            raise FirmwareFlashError("U-Boot Web 地址缺少主机名。")
        return http.client.HTTPConnection(host, parsed.port or 80, timeout=self.timeout)

    def probe_version(self, *, required: bool = True) -> Optional[str]:
        """Confirm the endpoint is U-Boot rather than a normal router Web UI."""

        connection = self._connection()
        try:
            connection.request("GET", "/version", headers={"Connection": "close"})
            response = connection.getresponse()
            body = response.read(4096).decode("utf-8", errors="replace").strip()
        except (OSError, http.client.HTTPException) as exc:
            if required:
                raise FirmwareFlashError(f"无法连接 U-Boot Web {self.web_url}: {exc}") from exc
            return None
        finally:
            connection.close()
        if response.status == 200 and body.startswith("U-Boot"):
            return body
        if required:
            raise FirmwareFlashError(
                f"{self.web_url} 不是兼容的 U-Boot Web（HTTP {response.status}）。"
            )
        return None

    @staticmethod
    def _decode_json(status: int, body: bytes, action: str) -> Dict[str, Any]:
        text = body.decode("utf-8", errors="replace").strip()
        if status != 200:
            raise FirmwareFlashError(f"U-Boot {action}返回 HTTP {status}: {text[:500]}")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise FirmwareFlashError(f"U-Boot {action}返回的不是 JSON: {text[:500]}") from exc
        if not isinstance(payload, dict):
            raise FirmwareFlashError(f"U-Boot {action}返回格式异常。")
        return payload

    def _post_file(self, field: str, path: Path) -> Dict[str, Any]:
        boundary = "----JDBoxAthena" + secrets.token_hex(12)
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        total = len(prefix) + path.stat().st_size + len(suffix)
        connection = self._connection()
        sent = 0
        next_percent = 10
        try:
            connection.putrequest("POST", "/upload")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(total))
            connection.putheader("Connection", "close")
            connection.endheaders()
            connection.send(prefix)
            with path.open("rb") as handle:
                while True:
                    block = handle.read(UPLOAD_CHUNK_SIZE)
                    if not block:
                        break
                    connection.send(block)
                    sent += len(block)
                    percent = int(sent * 100 / path.stat().st_size)
                    if percent >= next_percent:
                        LOGGER.info(
                            "固件上传到 U-Boot 内存: %d%% (%s/%s)",
                            min(percent, 100),
                            human_size(sent),
                            human_size(path.stat().st_size),
                        )
                        next_percent += 10
            connection.send(suffix)
            response = connection.getresponse()
            body = response.read(HTTP_RESPONSE_LIMIT)
            return self._decode_json(response.status, body, "上传校验")
        except (OSError, http.client.HTTPException) as exc:
            raise FirmwareFlashError(f"上传 Factory 固件失败（尚未写入闪存）: {exc}") from exc
        finally:
            connection.close()

    def _post_result(self, auto_reboot: bool) -> Dict[str, Any]:
        boundary = "----JDBoxAthena" + secrets.token_hex(12)
        value = "true" if auto_reboot else "false"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="auto_reboot"\r\n\r\n'
            f"{value}\r\n"
            f"--{boundary}--\r\n"
        ).encode("ascii")
        connection = self._connection()
        try:
            connection.request(
                "POST",
                "/result",
                body=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            payload = response.read(HTTP_RESPONSE_LIMIT)
            return self._decode_json(response.status, payload, "刷写")
        except (OSError, http.client.HTTPException) as exc:
            raise FirmwareFlashError(
                "已提交刷写请求但未能取得最终响应，状态未知。切勿断电或重复提交；"
                "请保持供电至少 10 分钟，再检查 U-Boot Web/系统日志。"
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _require_success(payload: Mapping[str, Any], action: str) -> Mapping[str, Any]:
        if payload.get("status") != "success":
            raise FirmwareFlashError(
                f"U-Boot {action}拒绝: {json.dumps(payload, ensure_ascii=False)}"
            )
        info = payload.get("info")
        if not isinstance(info, dict):
            raise FirmwareFlashError(f"U-Boot {action}成功响应缺少 info。")
        return info

    def flash(self, confirm: ConfirmationCallback, *, auto_reboot: bool = False) -> Path:
        """Run all guards and commit the U-Boot write only after confirmation."""

        image = self.validate_image()
        backup = self.validate_backup()
        version = self.probe_version(required=True)
        assert version is not None
        self.report.update(
            {
                "status": "local-preflight-complete",
                "image": asdict(image),
                "backup": asdict(backup),
                "web_url": self.web_url,
                "uboot_version": version,
                "auto_reboot": auto_reboot,
            }
        )
        self._write_report()
        LOGGER.info("本地预检完成，开始上传 Factory 固件到 U-Boot 内存（此阶段不写闪存）")
        upload_payload = self._post_file("firmware", Path(image.path))
        upload_info = self._require_success(upload_payload, "上传校验")
        try:
            remote_size = int(str(upload_info.get("size", "")))
        except ValueError as exc:
            raise FirmwareFlashError("U-Boot 上传响应中的固件大小无效。") from exc
        remote_md5 = str(upload_info.get("md5", "")).lower()
        remote_type = str(upload_info.get("type", ""))
        if remote_size != image.size_bytes or remote_md5 != image.md5:
            raise FirmwareFlashError("U-Boot 内存中的固件大小或 MD5 与本地不一致，禁止写入。")
        if remote_type != "FIT Image":
            raise FirmwareFlashError(f"U-Boot 将固件识别为 {remote_type!r}，不是 FIT Image。")
        phrase = "FLASH-FIRMWARE-" + image.sha256[:12].upper()
        plan = FirmwareFlashPlan(
            image=image,
            backup=backup,
            web_url=self.web_url,
            uboot_version=version,
            upload_info=dict(upload_info),
            write_targets=("0:HLOS", "rootfs", "BOOTCONFIG firmware slot 0"),
            confirmation_phrase=phrase,
            auto_reboot=auto_reboot,
        )
        self.report.update(
            {
                "status": "remote-validation-complete",
                "upload_response": upload_payload,
                "write_targets": list(plan.write_targets),
                "confirmation_phrase": phrase,
            }
        )
        self._write_report()
        if not confirm(plan):
            self.report["status"] = "cancelled-before-write"
            self._write_report()
            raise FirmwareFlashError("用户未确认；固件只在 U-Boot 内存中，未写入闪存。")
        self.report["status"] = "write-request-submitted"
        self._write_report()
        LOGGER.warning("正在由 U-Boot 写入 0:HLOS/rootfs；现在绝对不要断电")
        try:
            result_payload = self._post_result(auto_reboot)
        except FirmwareFlashError as exc:
            self.report.update({"status": "write-result-unknown", "error": str(exc)})
            self._write_report()
            raise
        self._require_success(result_payload, "刷写")
        self.report.update({"status": "complete", "result_response": result_payload})
        self._write_report()
        return self.report_path
