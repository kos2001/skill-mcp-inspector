"""Bounded, read-only MCP smoke probes (Python stdlib, macOS/Linux)."""
from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-11-25"
MODERN_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", PROTOCOL_VERSION}
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PAGES = 100


class ProbeError(Exception):
    def __init__(self, message: str, status: str = "error"):
        super().__init__(message)
        self.status = status


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise ProbeError("probe deadline exceeded", "timeout")
    return seconds


def response_result(message: Any, request_id: int) -> dict:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
        raise ProbeError("invalid JSON-RPC response")
    if "error" in message:
        err = message["error"]
        code = err.get("code") if isinstance(err, dict) else "invalid"
        raise ProbeError(f"JSON-RPC error {code}")
    if not isinstance(message.get("result"), dict):
        raise ProbeError("JSON-RPC result must be an object")
    if message["result"].get("resultType", "complete") != "complete":
        raise ProbeError("request requires an unsupported result interaction")
    return message["result"]


def request_params(channel, params: dict) -> dict:
    if getattr(channel, "protocol_version", None) == MODERN_VERSION:
        return {**params, "_meta": {
            "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "mcp-skill-inspector", "version": "0.2"},
        }}
    return params


def inspect_connection(channel, era: str = "legacy") -> dict[str, Any]:
    """Initialize, negotiate capabilities, and count all tools without invoking them."""
    if era == "modern":
        channel.protocol_version = MODERN_VERSION
        discovery = channel.request("server/discover", {})
        supported = discovery.get("supportedVersions")
        if not isinstance(supported, list) or MODERN_VERSION not in supported:
            raise ProbeError("server does not support the requested modern protocol")
        meta = discovery.get("_meta", {})
        if not isinstance(meta, dict):
            raise ProbeError("invalid discovery metadata")
        init = {"protocolVersion": MODERN_VERSION, "capabilities": discovery.get("capabilities"),
                "serverInfo": meta.get("io.modelcontextprotocol/serverInfo", {})}
    else:
        init = channel.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "mcp-skill-inspector", "version": "0.2"},
        })
    version = init.get("protocolVersion")
    if not isinstance(version, str) or version not in SUPPORTED_VERSIONS | ({MODERN_VERSION} if era == "modern" else set()):
        raise ProbeError(f"unsupported negotiated protocol version: {version}")
    capabilities = init.get("capabilities")
    if not isinstance(capabilities, dict) or not isinstance(init.get("serverInfo"), dict):
        raise ProbeError("initialize lacks capabilities or serverInfo")
    channel.protocol_version = version
    if era == "legacy":
        channel.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    result = {"status": "ok", "protocol_version": version,
              "server_info": init["serverInfo"], "capabilities": capabilities,
              "tool_count": 0, "tool_names": []}
    if "tools" not in capabilities:
        return result  # Resources/prompts-only MCP servers are healthy too.
    try:
        cursor = None
        seen = set()
        for _ in range(MAX_PAGES):
            page = channel.request("tools/list", {"cursor": cursor} if cursor else {})
            tools = page.get("tools")
            if not isinstance(tools, list) or any(
                not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in tools
            ):
                raise ProbeError("tools/list returned invalid tools")
            result["tool_count"] += len(tools)
            result["tool_names"].extend(t["name"] for t in tools[:30 - len(result["tool_names"])])
            cursor = page.get("nextCursor")
            if cursor is None:
                return result
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ProbeError("invalid or repeated tools/list cursor")
            seen.add(cursor)
        raise ProbeError("tools/list exceeded page limit")
    except (ProbeError, OSError, ValueError) as exc:
        result.update(status="partial", error=str(exc))
        return result


class StdioChannel:
    def __init__(self, process: subprocess.Popen, deadline: float):
        self.process = process
        self.deadline = deadline
        self.request_id = 0
        self.buffer = b""
        self.stderr_tail = b""
        self.selector = selectors.DefaultSelector()
        for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, label)
        os.set_blocking(process.stdin.fileno(), False)

    def send(self, message: dict) -> None:
        data = memoryview((json.dumps(message) + "\n").encode())
        with selectors.DefaultSelector() as writer:
            writer.register(self.process.stdin, selectors.EVENT_WRITE)
            while data:
                if not writer.select(remaining(self.deadline)):
                    raise ProbeError("stdin write timed out", "timeout")
                try:
                    data = data[os.write(self.process.stdin.fileno(), data):]
                except BlockingIOError:
                    continue

    def request(self, method: str, params: dict) -> dict:
        self.request_id += 1
        target = self.request_id
        self.send({"jsonrpc": "2.0", "id": target, "method": method, "params": request_params(self, params)})
        while True:
            remaining(self.deadline)
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise ProbeError("stdout contains non-JSON protocol data") from exc
                if not isinstance(message, dict):
                    raise ProbeError("stdout contains a non-object message")
                if "method" in message and "id" in message:
                    reply = {"jsonrpc": "2.0", "id": message["id"]}
                    if message["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": "client capability not supported"}
                    self.send(reply)
                elif message.get("id") == target:
                    return response_result(message, target)
            events = self.selector.select(remaining(self.deadline))
            if not events:
                raise ProbeError("no response before deadline", "timeout")
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.data == "stdout":
                        raise ProbeError("process closed stdout before responding", "exited")
                elif key.data == "stderr":
                    self.stderr_tail = (self.stderr_tail + chunk)[-4096:]
                else:
                    self.buffer += chunk
                    if len(self.buffer) > MAX_RESPONSE_BYTES:
                        raise ProbeError("stdout response exceeded size limit")


def stop_process(proc: subprocess.Popen) -> None:
    if proc.stdin:
        proc.stdin.close()
    try:
        proc.wait(timeout=0.15)
    except subprocess.TimeoutExpired:
        pass
    # Kill the isolated process group too: wrappers may leave children holding pipes.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=0.15)
        except subprocess.TimeoutExpired:
            continue
    proc.wait(timeout=1)
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()


def probe_stdio(server, timeout: float) -> dict[str, Any]:
    if os.name != "posix":
        return {"status": "skipped", "error": "stdio probes require macOS/Linux"}
    command = server.command
    if command and "/" in command and not Path(command).is_absolute():
        command = str(Path(server._cwd or Path.cwd()) / command)
    if not command or not shutil.which(command, path=server._env.get("PATH", os.environ.get("PATH"))):
        return {"status": "skipped", "error": "command not on PATH"}
    start = time.monotonic()
    channel = None
    proc = None
    result = {}
    try:
        proc = subprocess.Popen([command, *server.args], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                cwd=server._cwd, env={**os.environ, **server._env},
                                start_new_session=True)
        channel = StdioChannel(proc, start + timeout)
        result = inspect_connection(channel, server._protocol_era)
    except ProbeError as exc:
        result = {"status": exc.status, "error": str(exc)}
    except (OSError, ValueError) as exc:
        result = {"status": "error", "error": str(exc)}
    finally:
        if channel:
            try:
                while True:
                    chunk = os.read(proc.stderr.fileno(), 65536)
                    if not chunk:
                        break
                    channel.stderr_tail = (channel.stderr_tail + chunk)[-4096:]
                    if time.monotonic() > start + timeout:
                        break
            except (BlockingIOError, OSError):
                pass
            channel.selector.close()
            if channel.stderr_tail:
                result["stderr_tail"] = channel.stderr_tail.decode("utf-8", errors="replace")[-400:]
        if proc:
            stop_process(proc)
        result["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ProbeError("HTTP redirect refused; configure the final MCP URL")


class HttpChannel:
    def __init__(self, url: str, headers: dict[str, str], deadline: float):
        self.url = url
        self.headers = headers
        self.deadline = deadline
        self.protocol_version = None
        self.session_id = None
        self.request_id = 0
        self.http_status = None
        self.opener = urllib.request.build_opener(NoRedirect())

    def send(self, message: dict) -> dict | None:
        headers = {**self.headers, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": "mcp-skill-inspector/0.2"}
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.protocol_version == MODERN_VERSION:
            headers["Mcp-Method"] = message["method"]
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, json.dumps(message).encode(), headers, method="POST")
        try:
            with self.opener.open(req, timeout=remaining(self.deadline)) as resp:
                self.http_status = resp.status
                if message.get("method") == "initialize":
                    self.session_id = resp.headers.get("MCP-Session-Id")
                if "id" not in message:
                    if resp.status != 202:
                        raise ProbeError("notification was not accepted with HTTP 202")
                    return None
                content_type = resp.headers.get_content_type()
                if content_type not in ("application/json", "text/event-stream"):
                    raise ProbeError("HTTP endpoint did not return MCP JSON or SSE")
                buffer = b""
                total = 0
                while True:
                    budget = remaining(self.deadline)
                    # urllib's read timeout is per read; keep the overall deadline as well.
                    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                    if sock:
                        sock.settimeout(budget)
                    chunk = resp.read1(65536)
                    remaining(self.deadline)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise ProbeError("HTTP response exceeded size limit")
                    buffer += chunk
                    if content_type == "text/event-stream":
                        buffer = buffer.replace(b"\r\n", b"\n")
                        while b"\n\n" in buffer:
                            event, buffer = buffer.split(b"\n\n", 1)
                            data = b"\n".join(line[5:].lstrip(b" ") for line in event.split(b"\n") if line.startswith(b"data:"))
                            if not data:
                                continue
                            obj = json.loads(data)
                            if isinstance(obj, dict) and obj.get("id") == message["id"] and "method" not in obj:
                                return response_result(obj, message["id"])
                if content_type == "application/json":
                    return response_result(json.loads(buffer), message["id"])
                raise ProbeError("SSE stream ended without the requested response")
        except urllib.error.HTTPError as exc:
            self.http_status = exc.code
            exc.close()
            status = "auth_required" if exc.code == 401 else "forbidden" if exc.code == 403 else "error"
            raise ProbeError(f"HTTP {exc.code}", status) from exc

    def request(self, method: str, params: dict) -> dict:
        self.request_id += 1
        return self.send({"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": request_params(self, params)})

    def close(self) -> None:
        if self.session_id and self.protocol_version and self.deadline > time.monotonic():
            req = urllib.request.Request(self.url, method="DELETE", headers={
                **self.headers, "MCP-Session-Id": self.session_id,
                "MCP-Protocol-Version": self.protocol_version})
            try:
                with self.opener.open(req, timeout=min(remaining(self.deadline), 0.2)):
                    pass
            except (OSError, ProbeError, ValueError):
                pass


def probe_http(server, timeout: float) -> dict[str, Any]:
    if not server.url:
        return {"status": "skipped", "error": "no URL"}
    start = time.monotonic()
    channel = HttpChannel(server.url, server._headers, start + timeout)
    try:
        if server.transport == "sse":
            req = urllib.request.Request(server.url, headers={**server._headers, "Accept": "text/event-stream"})
            with channel.opener.open(req, timeout=timeout) as resp:
                result = {"status": "reachable", "http_status": resp.status,
                          "error": "legacy SSE reachability only; MCP handshake not verified"}
        else:
            result = inspect_connection(channel, server._protocol_era)
    except ProbeError as exc:
        result = {"status": exc.status, "error": str(exc)}
    except urllib.error.HTTPError as exc:
        result = {"status": "auth_required" if exc.code == 401 else "forbidden" if exc.code == 403 else "error",
                  "http_status": exc.code, "error": f"HTTP {exc.code}"}
        exc.close()
    except TimeoutError:
        result = {"status": "timeout", "error": "HTTP deadline exceeded"}
    except urllib.error.URLError as exc:
        result = {"status": "timeout" if isinstance(exc.reason, TimeoutError) else "unreachable", "error": str(exc.reason)}
    except (OSError, ValueError) as exc:
        result = {"status": "error", "error": str(exc)}
    finally:
        channel.close()
    if channel.http_status is not None:
        result["http_status"] = channel.http_status
    result["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
    return result
