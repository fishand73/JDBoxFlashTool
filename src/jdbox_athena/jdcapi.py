"""Authenticated ``/jdcapi`` access and Telnet-enablement strategies."""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urljoin

from .constants import ANONYMOUS_RPC_SESSION
from .errors import RpcError
from .util import wait_for_tcp

LOGGER = logging.getLogger(__name__)


class JdcApiClient:
    """Minimal JSON-RPC client for the router's local management API."""

    def __init__(self, management_url: str, timeout: float = 8.0) -> None:
        self.endpoint = urljoin(management_url, "jdcapi")
        self.timeout = timeout
        self.session: Optional[str] = None
        self._request_id = 0

    def _call(
        self,
        session: str,
        namespace: str,
        method: str,
        arguments: Mapping[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "call",
            "params": [session, namespace, method, dict(arguments)],
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "JDBox-Athena-Backup/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read()
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise RpcError(f"/jdcapi 请求失败: {exc}") from exc
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpcError("/jdcapi 返回的内容不是有效 JSON。") from exc
        if not isinstance(decoded, dict):
            raise RpcError("/jdcapi 返回格式异常。")
        if decoded.get("error"):
            raise RpcError(f"/jdcapi JSON-RPC 错误: {decoded['error']}")
        result = decoded.get("result")
        if not isinstance(result, list) or not result:
            raise RpcError(f"/jdcapi 缺少 result: {decoded}")
        status = result[0]
        if status != 0:
            raise RpcError(f"/jdcapi 调用失败，状态码 {status}: {decoded}")
        if len(result) < 2 or result[1] is None:
            return {}
        if not isinstance(result[1], dict):
            raise RpcError(f"/jdcapi result 数据格式异常: {decoded}")
        return dict(result[1])

    def login(self, username: str, password: str) -> str:
        """Exchange the management password for a temporary ubus session."""

        response = self._call(
            ANONYMOUS_RPC_SESSION,
            "session",
            "login",
            {"username": username, "password": password},
        )
        session = response.get("ubus_rpc_session")
        if not isinstance(session, str) or not session:
            raise RpcError("登录返回中没有 ubus_rpc_session，请检查后台用户名和密码。")
        self.session = session
        return session

    def call_static(self, method: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        """Call one authenticated ``jdcapi.static`` method."""

        if self.session is None:
            raise RpcError("必须先调用 session/login。")
        return self._call(self.session, "jdcapi.static", method, arguments)

    def _attempt_commands(self, strategy: str) -> List[str]:
        """Invoke the two fixed commands using one known firmware strategy."""

        errors: List[str] = []
        if strategy == "set_port_forward":
            commands = ("factory_hm info telnet 1", "telnetd")
        elif strategy == "set_iptv_info":
            commands = ("factory_hm info telnet 1", "telnetd -F -l /bin/login")
        else:  # pragma: no cover - internal programming error
            raise ValueError(f"Unknown strategy: {strategy}")
        for command in commands:
            if strategy == "set_port_forward":
                method = "set_port_forward"
                arguments: Dict[str, Any] = {
                    "on-off": 1,
                    "name": "t1",
                    "proto": "TCP",
                    "src-dport": f"1234;`{command}`",
                    "ipaddr": "192.168.1.100",
                    "dest-port": "80",
                    "edit": 1,
                }
            elif strategy == "set_iptv_info":
                method = "set_iptv_info"
                arguments = {
                    "enable": "1",
                    "vlan_enable": "1",
                    "vid": f"99; {command};",
                    "priority": "0",
                    "port": "1",
                }
            try:
                self.call_static(method, arguments)
            except RpcError as exc:
                # Starting telnetd can interrupt or outlive the HTTP request. The
                # TCP port probe below is the source of truth, not the RPC reply.
                errors.append(str(exc))
                LOGGER.debug("%s returned while running %r: %s", strategy, command, exc)
        return errors

    def enable_telnet(self, host: str, port: int, wait_seconds: float = 20.0) -> str:
        """Try r4546 first, then the legacy IPTV fallback.

        Success is only reported after a real TCP connection to the Telnet port.
        """

        failures: List[str] = []
        for strategy in ("set_port_forward", "set_iptv_info"):
            LOGGER.info("尝试 Telnet 开启策略: %s", strategy)
            rpc_errors = self._attempt_commands(strategy)
            if wait_for_tcp(host, port, timeout=wait_seconds):
                return strategy
            detail = "; ".join(rpc_errors) if rpc_errors else "RPC 已返回，但端口仍关闭"
            failures.append(f"{strategy}: {detail}")
        joined = "\n  - ".join(failures)
        raise RpcError(
            "已尝试 r4546 set_port_forward 与 legacy set_iptv_info，"
            f"但 TCP {host}:{port} 仍未开放。\n  - {joined}"
        )
