"""Guarded fallback to the locked JDCOS r4211 image through the stock Web API."""

from __future__ import annotations

import http.client
import json
import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urljoin, urlparse

from .constants import (
    DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_MD5,
    DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SHA256,
    DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SIZE,
)
from .errors import OfficialFirmwareUpgradeError, OperationCancelled, RpcError
from .integrity import hash_file
from .jdcapi import JdcApiClient
from .util import human_size, is_tcp_open

LOGGER = logging.getLogger(__name__)
HTTP_RESPONSE_LIMIT = 64 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024
FIT_MAGIC = b"\xd0\x0d\xfe\xed"


@dataclass(frozen=True)
class OfficialFirmwareInfo:
    """Metadata locked to the tested JDCOS r4211 recovery image."""

    path: str
    filename: str
    release: str
    size_bytes: int
    fit_size_bytes: int
    md5: str
    sha256: str


@dataclass(frozen=True)
class OfficialUpgradePlan:
    """Validated details shown before the stock firmware write begins."""

    image: OfficialFirmwareInfo
    management_url: str
    device_type: str
    current_release: str
    upload_info: Mapping[str, Any]
    firmware_check_status: int
    confirmation_phrase: str
    writes_boot_chain: bool = True


class OfficialFirmwareUpgrader:
    """Upload, validate, and start a fixed original-firmware recovery."""

    def __init__(
        self,
        management_url: str,
        image_path: Path,
        output: Path,
        username: str,
        password: str,
        *,
        request_timeout: float = 120.0,
        reboot_timeout: float = 600.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.management_url = management_url
        self.image_path = image_path
        self.output = output
        self.username = username
        self.password = password
        self.request_timeout = request_timeout
        self.reboot_timeout = reboot_timeout
        self.cancel_event = cancel_event
        self.client = JdcApiClient(management_url, timeout=request_timeout)
        self.report_path = output / "official-firmware-upgrade-report.json"
        self.report: Dict[str, Any] = {
            "schema_version": 1,
            "operation": "official-firmware-recovery",
            "status": "created",
            "target_release": "4.3.0.r4211",
        }
        self._plan: Optional[OfficialUpgradePlan] = None

    def _write_report(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise OperationCancelled("已取消原厂固件上传；尚未提交固件写入。")

    def validate_image(self) -> OfficialFirmwareInfo:
        """Accept only the exact user-tested signed r4211 image."""

        path = self.image_path.expanduser().resolve()
        if not path.is_file():
            raise OfficialFirmwareUpgradeError(f"找不到原厂恢复固件：{path}")
        size = path.stat().st_size
        if size != DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SIZE:
            raise OfficialFirmwareUpgradeError(
                "原厂恢复固件大小不符："
                f"{size} != {DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SIZE} bytes"
            )
        with path.open("rb") as handle:
            header = handle.read(64)
        if len(header) < 8 or header[:4] != FIT_MAGIC:
            raise OfficialFirmwareUpgradeError("原厂恢复固件不是预期的签名 FIT 镜像。")
        fit_size = int.from_bytes(header[4:8], "big")
        if fit_size < 4096 or fit_size > size:
            raise OfficialFirmwareUpgradeError(f"原厂恢复固件 FIT 声明大小异常：{fit_size}")
        md5, sha256 = hash_file(path)
        if md5.lower() != DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_MD5:
            raise OfficialFirmwareUpgradeError(f"原厂恢复固件 MD5 不匹配：{md5}")
        if sha256.lower() != DEFAULT_OFFICIAL_RECOVERY_FIRMWARE_SHA256:
            raise OfficialFirmwareUpgradeError(f"原厂恢复固件 SHA256 不匹配：{sha256}")
        return OfficialFirmwareInfo(
            path=str(path),
            filename=path.name,
            release="4.3.0.r4211",
            size_bytes=size,
            fit_size_bytes=fit_size,
            md5=md5.lower(),
            sha256=sha256.lower(),
        )

    def _connection(self) -> tuple[http.client.HTTPConnection, str]:
        endpoint = urlparse(urljoin(self.management_url, "cgi-bin/luci-upload"))
        if endpoint.hostname is None:
            raise OfficialFirmwareUpgradeError("原厂管理地址缺少主机名。")
        port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
        connection_class = (
            http.client.HTTPSConnection
            if endpoint.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(endpoint.hostname, port, timeout=self.request_timeout)
        path = endpoint.path or "/cgi-bin/luci-upload"
        if endpoint.query:
            path += "?" + endpoint.query
        return connection, path

    @staticmethod
    def _form_field(boundary: str, name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    def _upload(self, image: OfficialFirmwareInfo) -> Dict[str, Any]:
        session = self.client.session
        if not session:
            raise OfficialFirmwareUpgradeError("原厂固件上传前没有有效管理会话。")
        path = Path(image.path)
        boundary = "----JDBoxAthena" + secrets.token_hex(12)
        prefix = b"".join(
            (
                self._form_field(boundary, "sessionid", session),
                self._form_field(boundary, "filename", "/tmp/firmware.img"),
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="filedata"; filename="{path.name}"\r\n'
                    "Content-Type: application/octet-stream\r\n\r\n"
                ).encode("utf-8"),
            )
        )
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        total = len(prefix) + image.size_bytes + len(suffix)
        connection, endpoint = self._connection()
        sent = 0
        next_percent = 10
        self._check_cancelled()
        try:
            connection.putrequest("POST", endpoint)
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(total))
            connection.putheader("Accept", "application/json")
            connection.putheader("Connection", "close")
            connection.endheaders()
            connection.send(prefix)
            with path.open("rb") as handle:
                while True:
                    self._check_cancelled()
                    block = handle.read(UPLOAD_CHUNK_SIZE)
                    if not block:
                        break
                    connection.send(block)
                    sent += len(block)
                    percent = int(sent * 100 / image.size_bytes)
                    if percent >= next_percent:
                        LOGGER.info(
                            "原厂固件上传：%d%%（%s/%s）",
                            min(percent, 100),
                            human_size(sent),
                            human_size(image.size_bytes),
                        )
                        next_percent += 10
            self._check_cancelled()
            connection.send(suffix)
            response = connection.getresponse()
            body = response.read(HTTP_RESPONSE_LIMIT)
        except OperationCancelled:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise OfficialFirmwareUpgradeError(f"上传原厂恢复固件失败（尚未写入）：{exc}") from exc
        finally:
            connection.close()
        text = body.decode("utf-8", errors="replace").strip()
        if response.status != 200:
            raise OfficialFirmwareUpgradeError(
                f"原厂固件上传返回 HTTP {response.status}：{text[:500]}"
            )
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise OfficialFirmwareUpgradeError(
                f"原厂固件上传返回的不是 JSON：{text[:500]}"
            ) from exc
        if not isinstance(payload, dict):
            raise OfficialFirmwareUpgradeError("原厂固件上传返回格式异常。")
        return dict(payload)

    @staticmethod
    def _status(payload: Mapping[str, Any], action: str) -> int:
        try:
            return int(str(payload.get("status", "")))
        except ValueError as exc:
            raise OfficialFirmwareUpgradeError(f"{action}返回了无效状态：{payload}") from exc

    def prepare(self) -> OfficialUpgradePlan:
        """Validate locally, upload to /tmp, and ask the router to verify the image."""

        self._check_cancelled()
        image = self.validate_image()
        self.report.update(
            {
                "status": "local-validation-complete",
                "management_url": self.management_url,
                "image": asdict(image),
            }
        )
        self._write_report()
        LOGGER.info("原厂 r4211 固件本地哈希校验通过，登录原厂管理接口")
        self.client.login(self.username, self.password)
        router_info = self.client.call_static("web_get_router_info", {})
        device_type = str(router_info.get("type", "")).strip().upper()
        current_release = str(router_info.get("version", "")).strip()
        if device_type != "RE-CS-02":
            raise OfficialFirmwareUpgradeError(
                f"原厂管理接口报告设备型号为 {device_type or '未知'}，不是 RE-CS-02；禁止升级。"
            )
        LOGGER.info("原厂管理接口确认设备：%s，当前版本：%s", device_type, current_release)
        upload = self._upload(image)
        try:
            remote_size = int(str(upload.get("size", "")))
        except ValueError as exc:
            raise OfficialFirmwareUpgradeError("上传响应中的固件大小无效。") from exc
        remote_checksum = str(upload.get("checksum", "")).lower()
        if remote_size != image.size_bytes or remote_checksum != image.md5:
            raise OfficialFirmwareUpgradeError(
                "路由器 /tmp 中的固件大小或 MD5 与本地不一致，禁止升级。"
            )
        self._check_cancelled()
        check = self.client.call_static("firmware_check", {})
        check_status = self._status(check, "firmware_check")
        if check_status not in {0, 100}:
            raise OfficialFirmwareUpgradeError(
                f"路由器拒绝 r4211 固件校验，状态码 {check_status}。"
            )
        phrase = "DOWNGRADE-R4211-" + image.sha256[:12].upper()
        plan = OfficialUpgradePlan(
            image=image,
            management_url=self.management_url,
            device_type=device_type,
            current_release=current_release,
            upload_info=dict(upload),
            firmware_check_status=check_status,
            confirmation_phrase=phrase,
        )
        self._plan = plan
        self.report.update(
            {
                "status": "router-validation-complete",
                "router_info": {
                    "type": device_type,
                    "version": current_release,
                },
                "upload_response": upload,
                "firmware_check_response": check,
                "confirmation_phrase": phrase,
            }
        )
        self._write_report()
        return plan

    def cancel_before_write(self) -> None:
        self.report["status"] = "cancelled-before-write"
        self._write_report()

    def _wait_until_ready(self) -> None:
        parsed = urlparse(self.management_url)
        if parsed.hostname is None:
            raise OfficialFirmwareUpgradeError("原厂管理地址缺少主机名。")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        deadline = time.monotonic() + self.reboot_timeout
        went_offline = False
        LOGGER.info("升级请求已接受，等待路由器重启；预计需要 3–5 分钟")
        while time.monotonic() < deadline:
            online = is_tcp_open(parsed.hostname, port, timeout=1.0)
            if not online:
                went_offline = True
            elif went_offline:
                LOGGER.info("路由器管理端口已重新上线，等待管理接口和密码可用")
                while time.monotonic() < deadline:
                    try:
                        probe = JdcApiClient(self.management_url, timeout=8.0)
                        probe.login(self.username, self.password)
                        return
                    except RpcError:
                        time.sleep(3.0)
                break
            time.sleep(1.0)
        if not went_offline:
            raise OfficialFirmwareUpgradeError(
                "等待路由器开始重启超时。不要重复提交升级；请检查路由器当前版本和运行状态。"
            )
        raise OfficialFirmwareUpgradeError(
            "路由器已重启并恢复网络，但管理登录在超时前仍不可用；"
            "固件可能已恢复出厂设置，请重新完成首次配置后再运行工具。"
        )

    def commit_and_wait(self, plan: OfficialUpgradePlan) -> Path:
        """Commit once, then wait for a complete reboot and authenticated Web API."""

        if self._plan is None or plan != self._plan:
            raise OfficialFirmwareUpgradeError("原厂固件升级计划不是本次校验生成的。")
        self.report["status"] = "write-request-submitted"
        self._write_report()
        LOGGER.warning("正在提交原厂全量固件升级；从现在起绝对不要断电或重复提交")
        try:
            response = self.client.call_static("local_upgrade_action", {})
        except RpcError as exc:
            self.report.update({"status": "write-result-unknown", "error": str(exc)})
            self._write_report()
            raise OfficialFirmwareUpgradeError(
                "已提交原厂固件升级，但连接在取得确认前中断，状态未知。"
                "不要断电或重复提交；请保持供电至少 10 分钟后检查版本。"
            ) from exc
        status = self._status(response, "local_upgrade_action")
        if status != 0:
            self.report.update(
                {"status": "write-rejected", "local_upgrade_action_response": response}
            )
            self._write_report()
            messages = {2: "固件平台不匹配", 6: "固件校验失败"}
            reason = messages.get(status, f"状态码 {status}")
            raise OfficialFirmwareUpgradeError(f"路由器拒绝本地升级：{reason}。")
        self.report.update(
            {"status": "upgrade-started", "local_upgrade_action_response": response}
        )
        self._write_report()
        self._wait_until_ready()
        self.report["status"] = "complete"
        self._write_report()
        return self.report_path
