"""麦当劳 MCP 的最小客户端（零依赖，仅用标准库）。

麦当劳中国 MCP Server 走 Streamable HTTP：
- 端点：https://mcp.mcd.cn
- 鉴权：Authorization: Bearer <MCP_TOKEN>
- 协议：JSON-RPC 2.0 over POST；响应可能是 application/json，也可能是 text/event-stream

只实现本项目需要的三个方法：initialize / tools/list / tools/call。
这样不做 pip install 也能跑，评审 clone 下来就能复现。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"


def load_local_secrets() -> None:
    """环境变量里没有 token 时，从项目根的 `.env` / `server.env` 读进来。

    这是给云端演示服务用的：部署沙箱只上传源码，没有在启动前注入环境变量的入口，
    token 只能落在文件里。两个文件都在 .gitignore 里，不会进公开仓库。
    本地开发照旧用环境变量，行为不变。
    """
    if os.environ.get("MCD_MCP_TOKEN"):
        return
    root = Path(__file__).resolve().parents[2]
    for name in (".env", "server.env"):
        path = root / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and val and not os.environ.get(key):
                os.environ[key] = val
        if os.environ.get("MCD_MCP_TOKEN"):
            return


class MCPError(RuntimeError):
    """MCP 调用失败。"""


class MCDMCPClient:
    def __init__(
        self,
        token: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 30.0,
    ) -> None:
        token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not token:
            load_local_secrets()
            token = os.environ.get("MCD_MCP_TOKEN", "")
        if not token:
            raise MCPError(
                "缺少 MCP Token。请设置环境变量 MCD_MCP_TOKEN，"
                "或在 https://open.mcd.cn/mcp 控制台申请后传入。"
            )
        self.token = token.strip()
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 0
        self._initialized = False

    # ---------- 传输层 ----------

    def _post(self, payload: dict) -> dict | None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise MCPError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise MCPError(f"网络错误: {exc.reason}") from exc

        if not raw.strip():
            return None
        if "text/event-stream" in ctype:
            return _parse_sse(raw, payload.get("id"))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 有些网关会不带 Content-Type 直接回 SSE
            if raw.lstrip().startswith(("event:", "data:")):
                return _parse_sse(raw, payload.get("id"))
            raise MCPError(f"无法解析响应: {raw[:200]}")

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = self._post(payload)
        if resp is None:
            return None
        if "error" in resp and resp["error"]:
            err = resp["error"]
            raise MCPError(f"{method} 失败: {err.get('code')} {err.get('message')}")
        return resp.get("result")

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload)

    # ---------- 协议层 ----------

    def initialize(self) -> dict:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "mcd-combo-solver", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized")
        self._initialized = True
        return result or {}

    def list_tools(self) -> list[dict]:
        if not self._initialized:
            self.initialize()
        tools = self._rpc("tools/list") or {}
        return tools.get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """调用工具，返回解析后的内容（优先取 structuredContent，其次拼 text）。"""
        if not self._initialized:
            self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not result:
            return None
        if result.get("isError"):
            raise MCPError(f"工具 {name} 返回错误: {_flatten_text(result)}")
        if "structuredContent" in result and result["structuredContent"]:
            return result["structuredContent"]
        return _smart_parse(_flatten_text(result))


# ---------- 辅助 ----------


def _parse_sse(raw: str, want_id: Any) -> dict | None:
    """从 SSE 流里挑出与请求 id 对应的 JSON-RPC 响应。"""
    fallback: dict | None = None
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        data_lines = [ln[5:].lstrip() for ln in block.split("\n") if ln.startswith("data:")]
        if not data_lines:
            continue
        chunk = "\n".join(data_lines).strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            msg = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("id") == want_id:
            return msg
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            fallback = msg
    return fallback


def _flatten_text(result: dict) -> str:
    parts = []
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _smart_parse(text: str) -> Any:
    """工具返回的 text 里经常是 JSON 字符串，尽量解析成结构体。"""
    stripped = (text or "").strip()
    if not stripped:
        return None
    if stripped[0] in "{[":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return stripped


def with_retry(fn, attempts: int = 3, base_delay: float = 1.5):
    """针对 429 限流做指数退避重试。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except MCPError as exc:
            last = exc
            if "429" not in str(exc) or i == attempts - 1:
                raise
            time.sleep(base_delay * (2**i))
    if last:
        raise last
