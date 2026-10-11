#!/usr/bin/env python3
"""mcp-skill-inspector — audit installed Skills and MCP servers.

Outputs a stability + external-dependency report in JSON and Markdown.

Usage:
    python scripts/audit.py [--out DIR] [--probe] [--probe-timeout SECS]
                      [--claude-home PATH] [--scope SCOPE]

Scopes:
    all       enumerate user-level (~/.claude) and project-level configs (default)
    user      only ~/.claude/**
    project   only ./.mcp.json, ./.claude/settings*.json, ./.claude/skills/**

--probe actually spawns each stdio MCP server and runs initialize+tools/list,
and performs Streamable HTTP handshakes (legacy SSE: reachability only).
Enumeration is offline; probes execute configured commands and contact endpoints.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ipaddress
import math
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from probes import probe_http, probe_stdio
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

# npm/pypi package names whose primary purpose is reaching an external SaaS.
# Match is by substring on args, case-insensitive.
EXTERNAL_PACKAGE_HINTS = {
    "linear", "github", "gitlab", "slack", "notion", "asana", "jira",
    "confluence", "atlassian", "zendesk", "intercom", "hubspot", "salesforce",
    "stripe", "shopify", "discord", "telegram", "gmail", "google",
    "calendar", "drive", "sheets", "openai", "anthropic", "tavily", "brave",
    "exa", "perplexity", "firecrawl", "upstash", "context7", "supabase",
    "vercel", "cloudflare", "aws", "azure", "gcp", "sentry", "datadog",
    "grafana", "pagerduty", "twilio", "sendgrid", "mailgun",
}

# Substrings that suggest the server stays on this machine.
LOCAL_PACKAGE_HINTS = {
    "filesystem", "sqlite", "memory", "fetch-local", "shell",
    "everything", "git-local",
}

# Markers in skill content that indicate outbound network use.
EXTERNAL_SKILL_PATTERNS = [
    (re.compile(r"\bWebFetch\b"), "uses WebFetch"),
    (re.compile(r"\bWebSearch\b"), "uses WebSearch"),
    (re.compile(r"\bmcp__[a-z0-9_]+__", re.I), "calls MCP tool"),
    (re.compile(r"\b(?:curl|wget)\b[^\n]*https?://", re.I), "shells out to HTTP client"),
    (re.compile(r"\b(?:requests|httpx)\.(?:get|post|put|delete|head|patch|request)\s*\(", re.I), "uses Python HTTP client"),
    (re.compile(r"\b(?:urlopen|urlretrieve)\s*\(|\burllib\.request\.build_opener\s*\(", re.I), "uses urllib HTTP client"),
    (re.compile(r"\bfetch\s*\(", re.I), "uses fetch()"),
    (re.compile(r"\bapi[._-]?key\b", re.I), "references an API key"),
    (re.compile(r"\bgh\s+(pr|issue|api|repo)\b"), "shells out to gh CLI"),
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class McpServer:
    name: str
    source: str           # path of config file it came from
    scope: str            # "user" | "project" | "plugin"
    transport: str        # "stdio" | "http" | "sse"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    env_keys: list[str] = field(default_factory=list)
    enabled: bool = True
    classification: str = "unknown"   # "local" | "external" | "unknown"
    reasons: list[str] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    effective: bool = True
    validation_errors: list[str] = field(default_factory=list)
    _env: dict[str, str] = field(default_factory=dict, repr=False)
    _headers: dict[str, str] = field(default_factory=dict, repr=False)
    _cwd: str | None = field(default=None, repr=False)
    _protocol_era: str = field(default="legacy", repr=False)


@dataclass
class Skill:
    name: str
    path: str
    scope: str            # "user" | "plugin" | "project"
    description: str = ""
    classification: str = "unknown"
    reasons: list[str] = field(default_factory=list)
    size_bytes: int = 0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    _text: str | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
MAX_SCAN_BYTES = 2 * 1024 * 1024


def diagnostic(issues: list[dict] | None, path: Path, message: str) -> None:
    if issues is not None:
        issues.append({"path": str(path), "message": message})


def load_json(path: Path, issues: list[dict] | None = None) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            diagnostic(issues, path, "configuration root must be an object")
            return None
        return data
    except json.JSONDecodeError as exc:
        diagnostic(issues, path, f"invalid JSON at line {exc.lineno}, column {exc.colno}")
    except (OSError, UnicodeError) as exc:
        diagnostic(issues, path, f"could not read configuration: {exc.__class__.__name__}")
    return None


def walk_files(root: Path, issues: list[dict] | None = None):
    if not root.exists():
        return
    def onerror(exc):
        diagnostic(issues, Path(exc.filename or root), f"scan failed: {exc.__class__.__name__}")
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            yield Path(directory) / name


def discover_mcp_configs(claude_home: Path, project_root: Path, scope: str,
                         claude_config: Path | None = None,
                         issues: list[dict] | None = None) -> list[Path]:
    paths: list[Path] = []
    global_config = claude_config or claude_home.parent / ".claude.json"
    if global_config.exists():
        paths.append(global_config)  # project-only mode parses only this project's entry.
    if scope in ("all", "user"):
        paths.extend(p for p in (claude_home / "settings.json", claude_home / "settings.local.json") if p.exists())
        for sub in ("plugins/cache", "plugins/marketplaces"):
            paths.extend(p for p in walk_files(claude_home / sub, issues) if p.name == ".mcp.json")
    if scope in ("all", "project"):
        paths.extend(p for p in (project_root / ".mcp.json", project_root / ".claude/settings.json",
                                project_root / ".claude/settings.local.json") if p.exists())
    return sorted(set(paths), key=str)


VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_value(value: str, variables: dict[str, str]) -> str:
    def replace(match):
        key, default = match.groups()
        if variables.get(key):
            return variables[key]
        if default is not None:
            return default
        if key in variables:
            return variables[key]
        raise ValueError(f"missing environment variable: {key}")
    return VARIABLE_RE.sub(replace, value)


def project_settings(data: dict, project_root: Path) -> dict:
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        return {}
    entry = projects.get(str(project_root))
    if entry is None:
        entry = next((v for k, v in projects.items() if Path(k).expanduser().resolve() == project_root), {})
    return entry if isinstance(entry, dict) else {}


def parse_mcp_servers(config_paths: list[Path], disabled: set[str], *,
                      project_root: Path | None = None, scope: str = "all",
                      claude_config: Path | None = None, claude_home: Path | None = None,
                      issues: list[dict] | None = None, config_data: dict | None = None,
                      active_plugins: set[Path] | None = None,
                      rejected_project: set[str] | None = None) -> list[McpServer]:
    project_root = (project_root or Path.cwd()).resolve()
    servers: list[McpServer] = []
    for cfg in sorted(set(config_paths), key=str):
        data = config_data.get(cfg) if config_data is not None else load_json(cfg, issues)
        if data is None:
            continue
        plugin = bool((claude_home and cfg.is_relative_to(claude_home / "plugins")) or
                      any(cfg.is_relative_to(root) for root in (active_plugins or set())))
        label = "plugin" if plugin else "project" if cfg.is_relative_to(project_root) else "user"
        blocks = []
        if cfg == claude_config:
            if scope in ("all", "user"):
                blocks.append(("user", data.get("mcpServers", {})))
            if scope in ("all", "project"):
                entry = project_settings(data, project_root)
                if isinstance(entry, dict):
                    blocks.append(("local", entry.get("mcpServers", {})))
        else:
            blocks.append((label, data.get("mcpServers", {}) if cfg.name.startswith("settings") else data.get("mcpServers", data)))
        for label, block in blocks:
            if not isinstance(block, dict):
                diagnostic(issues, cfg, "mcpServers must be an object")
                continue
            for name, spec in sorted(block.items()):
                enabled = name not in disabled
                if isinstance(spec, dict):
                    enabled = enabled and spec.get("disabled") is not True and spec.get("enabled") is not False
                if cfg == project_root / ".mcp.json" and name in (rejected_project or set()):
                    enabled = False
                if plugin and not any(cfg.is_relative_to(root) for root in (active_plugins or set())):
                    enabled = False
                try:
                    if not isinstance(spec, dict):
                        raise ValueError(f"server {name}: definition must be an object")
                    command, args, url = spec.get("command"), spec.get("args", []), spec.get("url")
                    env, headers = spec.get("env", {}), spec.get("headers", {})
                    if command is not None and not isinstance(command, str):
                        raise ValueError(f"server {name}: command must be a string")
                    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
                        raise ValueError(f"server {name}: args must be a list of strings")
                    if url is not None and not isinstance(url, str):
                        raise ValueError(f"server {name}: URL must be a string")
                    for key, value in (("env", env), ("headers", headers)):
                        if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
                            raise ValueError(f"server {name}: {key} must contain string values")
                    transport = spec.get("type") or ("http" if url else "stdio")
                    if transport == "streamable-http":
                        transport = "http"
                    if transport not in ("stdio", "http", "sse", "ws", "sdk"):
                        raise ValueError(f"server {name}: unsupported transport")
                    variables = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_root)}
                    if plugin:
                        variables["CLAUDE_PLUGIN_ROOT"] = str(cfg.parent)
                    command = expand_value(command, variables) if command else None
                    args = [expand_value(a, variables) for a in args]
                    env = {k: expand_value(v, variables) for k, v in env.items()}
                    headers = {k: expand_value(v, variables) for k, v in headers.items()}
                    url = expand_value(url, variables) if url else None
                    if transport == "stdio" and not command:
                        raise ValueError(f"server {name}: stdio requires a command")
                    if transport in ("http", "sse", "ws"):
                        parsed = urllib.parse.urlsplit(url or "")
                        allowed = ("ws", "wss") if transport == "ws" else ("http", "https")
                        if parsed.scheme not in allowed or not parsed.hostname:
                            raise ValueError(f"server {name}: invalid transport URL")
                        _ = parsed.port  # Validate malformed ports before probing.
                    cwd = spec.get("cwd")
                    if cwd is not None and not isinstance(cwd, str):
                        raise ValueError(f"server {name}: cwd must be a string")
                    cwd = expand_value(cwd, variables) if cwd else str(cfg.parent if plugin else project_root)
                    cwd_path = Path(cwd).expanduser()
                    if not cwd_path.is_absolute():
                        cwd_path = (cfg.parent if plugin else project_root) / cwd_path
                    servers.append(McpServer(name=name, source=str(cfg), scope=label,
                                             transport=transport, command=command, args=args,
                                             url=url, env_keys=sorted(env), enabled=enabled,
                                             _env=env, _headers=headers, _cwd=str(cwd_path)))
                except ValueError as exc:
                    diagnostic(issues, cfg, str(exc))
                    servers.append(McpServer(name=name, source=str(cfg), scope=label,
                                             transport="unknown", enabled=enabled,
                                             validation_errors=[str(exc)]))
    ranks = {"plugin": 0, "user": 1, "project": 2, "local": 3}
    winners: dict[str, McpServer] = {}
    for server in servers:
        if server.scope == "plugin":
            continue  # Plugin names are client-namespaced; retain separate inventory entries.
        previous = winners.get(server.name)
        if previous and ranks[previous.scope] > ranks[server.scope]:
            server.effective = False
        else:
            if previous:
                previous.effective = False
            winners[server.name] = server
    return servers


def discover_skills(claude_home: Path, project_root: Path, scope: str,
                    issues: list[dict] | None = None) -> list[Skill]:
    skills = []
    roots = []
    if scope in ("all", "user"):
        roots.extend((claude_home / sub, "plugin" if "plugins" in sub else "user")
                     for sub in ("skills", "plugins/cache", "plugins/marketplaces"))
    if scope in ("all", "project"):
        roots.append((project_root / ".claude/skills", "project"))
    seen = set()
    for root, label in roots:
        for path in walk_files(root, issues):
            if path.name != "SKILL.md" or path.resolve() in seen:
                continue
            seen.add(path.resolve())
            try:
                size = path.stat().st_size
                if size > MAX_SCAN_BYTES:
                    diagnostic(issues, path, "SKILL.md exceeds scan size limit")
                    skills.append(Skill(name=path.parent.name, path=str(path), scope=label, size_bytes=size))
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                name, description = parse_frontmatter(text)
                skill = Skill(name=name or path.parent.name, path=str(path), scope=label,
                              description=description, size_bytes=size, _text=text)
                classify_skill(skill, issues)
                skill._text = None
                skills.append(skill)
            except OSError as exc:
                diagnostic(issues, path, f"could not read skill: {exc.__class__.__name__}")
    return skills


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def parse_frontmatter(text: str) -> tuple[str, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return "", ""
    block = m.group(1)
    name = ""
    desc = ""
    # ultra-simple YAML (single line values), good enough for skill frontmatter
    current_key = None
    buf: list[str] = []
    def flush():
        nonlocal name, desc, buf, current_key
        if current_key == "name":
            name = " ".join(buf).strip().strip("\"\'")
        elif current_key == "description":
            desc = " ".join(buf).strip().strip("\"\'")
        buf = []
    for line in block.splitlines():
        if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", line):
            flush()
            key, _, val = line.partition(":")
            current_key = key.strip()
            val = val.strip()
            if val and val not in (">", "|", ">-", "|-"):
                buf.append(val)
        else:
            buf.append(line.strip())
    flush()
    return name, desc


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def matching_hints(blob: str, hints: set[str]) -> list[str]:
    return [hint for hint in sorted(hints)
            if re.search(r"(?<![a-z0-9])" + re.escape(hint) + r"(?![a-z0-9])", blob)]


def classify_server(s: McpServer) -> None:
    reasons = []
    blob = " ".join([s.command or "", *s.args]).lower()
    external = matching_hints(blob, EXTERNAL_PACKAGE_HINTS)
    local = matching_hints(blob, LOCAL_PACKAGE_HINTS)
    remote = False
    loopback = False
    if s.url:
        host = urllib.parse.urlsplit(s.url).hostname or ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        remote = not loopback
        reasons.append("loopback HTTP endpoint" if loopback else "network endpoint outside loopback")
    if external:
        reasons.append(f"args reference external services: {', '.join(external)}")
    if local:
        reasons.append(f"args reference local capabilities: {', '.join(local)}")
    credentials = [k for k in s.env_keys if is_sensitive_key(k)]
    if credentials:
        reasons.append(f"credential-shaped env keys: {', '.join(credentials)}")
    if remote or external:
        s.classification = "external"
    elif credentials:
        s.classification = "unknown"  # Credentials alone do not prove outbound access.
    elif local or loopback:
        s.classification = "local"
    else:
        s.classification = "unknown"
        reasons.append("no reliable network or local capability markers")
    s.reasons = reasons


def classify_skill(skill: Skill, issues: list[dict] | None = None) -> None:
    evidence = []
    scan_issues = issues if issues is not None else []
    issue_count = len(scan_issues)
    path = Path(skill.path)
    sources = [(path, skill._text)]
    sources.extend((p, None) for p in walk_files(path.parent / "scripts", scan_issues)
                   if p.suffix in (".py", ".sh", ".js", ".ts", ".mjs", ".cjs", ".tsx"))
    incomplete = len(scan_issues) != issue_count
    for source, cached in sources:
        try:
            if source.stat().st_size > MAX_SCAN_BYTES:
                diagnostic(issues, source, "file exceeds scan size limit")
                incomplete = True
                continue
            text = cached if cached is not None else source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            diagnostic(issues, source, f"could not scan file: {exc.__class__.__name__}")
            incomplete = True
            continue
        for pattern, reason in EXTERNAL_SKILL_PATTERNS:
            match = pattern.search(text)
            if match:
                evidence.append({"path": str(source), "line": text.count("\n", 0, match.start()) + 1,
                                 "reason": reason})
    skill.evidence = evidence
    skill.reasons = sorted({e["reason"] for e in evidence})
    skill.classification = "external" if evidence else "unknown" if incomplete else "local"


SENSITIVE_KEY_RE = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|authorization|credential)", re.I)


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key)) or key.upper().endswith("_KEY")


def sanitize(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, dict):
        enums = {
            "classification": {"local", "external", "unknown"},
            "scope": {"local", "project", "user", "plugin", "all"},
            "transport": {"stdio", "http", "sse", "ws", "sdk", "unknown"},
            "status": {"ok", "partial", "disabled", "skipped", "config_error", "error",
                       "timeout", "exited", "auth_required", "forbidden", "reachable", "unreachable"},
        }
        return {k: v if k in enums and isinstance(v, str) and v in enums[k] else sanitize(v, secrets)
                for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        result = [sanitize(v, secrets) for v in value]
        for index, arg in enumerate(value[:-1]):
            if isinstance(arg, str) and arg.startswith("-") and is_sensitive_key(arg) and "=" not in arg:
                result[index + 1] = "[REDACTED]"
        return result
    if not isinstance(value, str):
        return value
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            if len(secret) < 4:
                value = re.sub(r"(?<![A-Za-z0-9])" + re.escape(secret) + r"(?![A-Za-z0-9])",
                               "[REDACTED]", value)
            else:
                value = value.replace(secret, "[REDACTED]")
    def clean_url(match):
        raw = match.group(0)
        try:
            parts = urllib.parse.urlsplit(raw)
            host = parts.netloc.rsplit("@", 1)[-1]
            query = urllib.parse.urlencode([(k, "[REDACTED]") for k, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)])
            return urllib.parse.urlunsplit((parts.scheme, host, parts.path, query, ""))
        except ValueError:
            return "[REDACTED URL]"
    value = re.sub(r"(?:https?|wss?)://[^\s\"'<>]+", clean_url, value)
    value = re.sub(r"(?i)(\b(?:[a-z0-9_-]*(?:token|secret|password|api[_-]?key)[a-z0-9_-]*)\s*[=:]\s*)[^\s,;]+",
                   lambda m: m.group(1) + "[REDACTED]", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
    return value


def report_secrets(servers: list[McpServer]) -> set[str]:
    values = {value for key, value in os.environ.items() if is_sensitive_key(key) and value}
    for server in servers:
        values.update(v for k, v in server._env.items() if is_sensitive_key(k) and v)
        values.update(v for v in server._headers.values() if v)
        for header in server._headers.values():
            if header.lower().startswith("bearer "):
                values.add(header[7:])
        for text in [server.url or "", *server.args]:
            for url in re.findall(r"https?://[^\s]+", text):
                try:
                    parsed = urllib.parse.urlsplit(url)
                except ValueError:
                    continue
                values.update(v for _, v in urllib.parse.parse_qsl(parsed.query) if v)
                if parsed.password:
                    values.add(parsed.password)
        for index, arg in enumerate(server.args):
            if arg.startswith("-") and is_sensitive_key(arg):
                if "=" in arg:
                    values.add(arg.split("=", 1)[1])
                elif index + 1 < len(server.args):
                    values.add(server.args[index + 1])
    return values


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------

def probe_server(s: McpServer, timeout: float) -> None:
    if not s.enabled:
        s.health = {"status": "disabled"}
    elif not s.effective:
        s.health = {"status": "skipped", "error": "shadowed by higher-precedence configuration"}
    elif s.validation_errors:
        s.health = {"status": "config_error", "error": "; ".join(s.validation_errors)}
    elif s.transport in ("http", "sse"):
        s.health = probe_http(s, timeout)
    elif s.transport == "stdio":
        s.health = probe_stdio(s, timeout)
    else:
        s.health = {"status": "skipped", "error": "unsupported transport"}


def run_probes(servers: list[McpServer], timeout: float, workers: int) -> None:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(probe_server, server, timeout) for server in servers]
        for future in futures:
            future.result()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_reports(out_dir: Path, servers: list[McpServer], skills: list[Skill],
                  meta: dict[str, Any], diagnostics: list[dict] | None = None,
                  baseline: dict | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "mcp_servers": [asdict(s) for s in servers],
        "skills": [asdict(s) for s in skills],
        "summary": summarize(servers, skills),
        "diagnostics": diagnostics or [],
    }
    payload = sanitize(payload, report_secrets(servers))
    if baseline is not None:
        payload["changes"] = compare_reports(baseline, payload)
        payload["changes"] = sanitize(payload["changes"], report_secrets(servers))
    atomic_write(out_dir / "report.md", render_markdown(payload))
    atomic_write(out_dir / "report.json", json.dumps(payload, indent=2, ensure_ascii=False))
    return payload


def atomic_write(path: Path, text: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".report-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def compare_reports(previous: dict, current: dict) -> dict:
    changes = {}
    for group, keys, fields in (
        ("mcp_servers", ("source", "scope", "name"), ("transport", "command", "args", "url", "env_keys", "enabled", "effective", "classification")),
        ("skills", ("path",), ("name", "classification", "size_bytes", "reasons")),
    ):
        before = {tuple(item[k] for k in keys): item for item in previous[group]}
        after = {tuple(item[k] for k in keys): item for item in current[group]}
        def identity(key):
            return dict(zip(keys, key))
        changes[group] = {
            "added": [identity(k) for k in sorted(after.keys() - before.keys())],
            "removed": [identity(k) for k in sorted(before.keys() - after.keys())],
            "changed": [],
        }
        for key in sorted(before.keys() & after.keys()):
            fields_changed = [f for f in fields if before[key].get(f) != after[key].get(f)]
            old_status = before[key].get("health", {}).get("status")
            new_status = after[key].get("health", {}).get("status")
            if old_status and new_status and old_status != new_status:
                fields_changed.append("health.status")
            if fields_changed:
                changes[group]["changed"].append({**identity(key), "fields": fields_changed})
    return changes


def summarize(servers: list[McpServer], skills: list[Skill]) -> dict[str, Any]:
    def by(field_, items):
        out: dict[str, int] = {}
        for it in items:
            out[getattr(it, field_)] = out.get(getattr(it, field_), 0) + 1
        return out
    healthy = sum(1 for s in servers if s.health.get("status") == "ok")
    probed = sum(1 for s in servers if s.health.get("status") not in (None, "disabled", "skipped", "config_error"))
    return {
        "server_count": len(servers),
        "server_classification": by("classification", servers),
        "server_transport": by("transport", servers),
        "server_health": {state: sum(s.health.get("status") == state for s in servers)
                          for state in sorted({s.health["status"] for s in servers if s.health.get("status")})},
        "servers_probed": probed,
        "servers_ok": healthy,
        "skill_count": len(skills),
        "skill_classification": by("classification", skills),
    }


def md(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ").replace("\r", " ")


def render_markdown(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    summary = payload["summary"]
    lines: list[str] = []
    lines.append("# MCP & Skill Inspector Report")
    lines.append("")
    lines.append(f"- Generated: {meta['generated_at']}")
    lines.append(f"- Scope: `{meta['scope']}`  Probe: `{meta['probe']}`")
    lines.append(f"- Claude home: `{md(meta['claude_home'])}`")
    lines.append(f"- Project root: `{md(meta['project_root'])}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- MCP servers: **{summary['server_count']}** "
                 f"(classification: {summary['server_classification']}, "
                 f"transport: {summary['server_transport']})")
    if summary["servers_probed"]:
        lines.append(f"- Health: {summary['servers_ok']}/{summary['servers_probed']} OK")
    lines.append(f"- Skills: **{summary['skill_count']}** "
                 f"(classification: {summary['skill_classification']})")
    lines.append("")
    if payload.get("diagnostics"):
        lines.extend(["## Scan diagnostics", ""])
        for issue in payload["diagnostics"]:
            lines.append(f"- `{md(issue['path'])}`: {md(issue['message'])}")
        lines.append("")
    if payload.get("changes"):
        lines.extend(["## Changes from baseline", ""])
        for group, diff in payload["changes"].items():
            lines.append(f"- {group}: {len(diff['added'])} added, {len(diff['removed'])} removed, {len(diff['changed'])} changed")
            for kind, entries in diff.items():
                for entry in entries:
                    lines.append(f"  - {kind}: {md(entry.get('name', entry.get('path')))} — {md(entry)}")
        lines.append("")
    lines.append("## MCP Servers")
    lines.append("")
    lines.append("| Name | Scope | Enabled | Effective | Transport | Class | Health | Latency | Source |")
    lines.append("|------|-------|---------|-----------|-----------|-------|--------|---------|--------|")
    for s in payload["mcp_servers"]:
        h = s.get("health") or {}
        status = h.get("status", "-")
        lat = f"{h.get('latency_ms','-')} ms" if h.get("latency_ms") is not None else "-"
        lines.append(f"| `{md(s['name'])}` | {s['scope']} | {s['enabled']} | {s['effective']} | {s['transport']} | "
                     f"**{s['classification']}** | {status} | {lat} | "
                     f"`{md(s['source'])}` |")
    lines.append("")
    ext_servers = [s for s in payload["mcp_servers"] if s["classification"] == "external"]
    if ext_servers:
        lines.append("### External MCP servers (touch the network)")
        for s in ext_servers:
            target = s["url"] or " ".join([s["command"] or "", *s["args"]])
            lines.append(f"- **{md(s['name'])}** → `{md(target)}`")
            for r in s["reasons"]:
                lines.append(f"  - {md(r)}")
            if s.get("env_keys"):
                lines.append(f"  - env: {md(s['env_keys'])}")
        lines.append("")
    failed = [s for s in payload["mcp_servers"]
              if (s.get("health") or {}).get("status") not in (None, "ok", "disabled", "skipped")]
    if failed:
        lines.append("### Unhealthy servers")
        for s in failed:
            h = s["health"]
            lines.append(f"- **{md(s['name'])}** — {h.get('status')}: {md(h.get('error',''))}")
        lines.append("")
    lines.append("## Skills")
    lines.append("")
    lines.append(f"Total: {len(payload['skills'])}")
    lines.append("")
    ext_skills = [s for s in payload["skills"] if s["classification"] == "external"]
    lines.append(f"### External skills ({len(ext_skills)})")
    lines.append("")
    for s in sorted(ext_skills, key=lambda x: x["name"]):
        lines.append(f"- **{md(s['name'])}** (`{s['scope']}`) — {md(', '.join(s['reasons']))}")
        lines.append(f"  - `{md(s['path'])}`")
        for evidence in s.get("evidence", []):
            lines.append(f"  - {md(evidence['reason'])}: `{md(evidence['path'])}:{evidence['line']}`")
    lines.append("")
    local_skills = [s for s in payload["skills"] if s["classification"] == "local"]
    lines.append(f"### Skills with no static external markers ({len(local_skills)})")
    lines.append("")
    for s in sorted(local_skills, key=lambda x: x["name"])[:50]:
        lines.append(f"- {md(s['name'])} (`{s['scope']}`)")
    if len(local_skills) > 50:
        lines.append(f"- … and {len(local_skills) - 50} more (see report.json)")
    unknown = [s for s in payload["skills"] if s["classification"] == "unknown"]
    if unknown:
        lines.extend(["", f"### Unknown skills ({len(unknown)})", ""])
        lines.extend(f"- {md(s['name'])}: `{md(s['path'])}`" for s in unknown)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def active_plugin_roots(claude_home: Path, project_root: Path, settings: list[dict],
                        issues: list[dict]) -> set[Path]:
    registry = claude_home / "plugins/installed_plugins.json"
    if not registry.exists():
        return set()
    data = load_json(registry, issues) or {}
    enabled = {}
    for config in settings:
        block = config.get("enabledPlugins", {})
        if isinstance(block, dict):
            enabled.update(block)
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        diagnostic(issues, registry, "plugins must be an object")
        return set()
    roots = set()
    for name, records in plugins.items():
        if enabled.get(name) is False:
            continue
        if not isinstance(records, list):
            diagnostic(issues, registry, "plugin installations must be lists")
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            install_scope = record.get("scope", "user")
            if install_scope in ("project", "local") and record.get("projectPath") != str(project_root):
                continue
            path = record.get("installPath")
            if isinstance(path, str):
                roots.add(Path(path).expanduser().resolve())
    return roots


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Inspect installed Skills and MCP servers")
    ap.add_argument("--out", default="./mcp-inspector-out")
    ap.add_argument("--probe", action="store_true", help="execute configured servers and contact endpoints")
    ap.add_argument("--probe-timeout", type=float, default=8.0, help="overall per-server probe budget, seconds")
    ap.add_argument("--workers", type=int, default=4, help="maximum concurrent probes (1-32, default: 4)")
    ap.add_argument("--protocol-era", choices=("legacy", "modern"), default="legacy",
                    help="legacy: initialize (through 2025-11-25); modern: server/discover (2026-07-28)")
    ap.add_argument("--claude-home", default=os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude")))
    ap.add_argument("--claude-config", help="override ~/.claude.json (default: sibling of Claude home)")
    ap.add_argument("--project-root", default=str(Path.cwd()))
    ap.add_argument("--config", action="append", default=[], help="additional MCP JSON file; repeatable")
    ap.add_argument("--server", action="append", default=[], help="select exact server name; repeatable")
    ap.add_argument("--scope", choices=("all", "user", "project"), default="all")
    ap.add_argument("--baseline", help="compare with a previous report.json")
    ap.add_argument("--fail-on-unhealthy", action="store_true", help="exit 1 when an enabled effective probe is not OK")
    ap.add_argument("--strict", action="store_true", help="exit 1 on scan/configuration diagnostics")
    args = ap.parse_args(argv)
    if not math.isfinite(args.probe_timeout) or args.probe_timeout <= 0:
        ap.error("--probe-timeout must be finite and positive")
    if not 1 <= args.workers <= 32:
        ap.error("--workers must be between 1 and 32")
    if args.fail_on_unhealthy and not args.probe:
        ap.error("--fail-on-unhealthy requires --probe")

    start = time.monotonic()
    claude_home = Path(args.claude_home).expanduser().resolve()
    claude_config = Path(args.claude_config).expanduser().resolve() if args.claude_config else claude_home.parent / ".claude.json"
    project_root = Path(args.project_root).expanduser().resolve()
    if not project_root.is_dir():
        ap.error("--project-root must be an existing directory")
    out_dir = Path(args.out).expanduser().resolve()
    issues: list[dict] = []
    baseline = None
    if args.baseline:
        baseline = load_json(Path(args.baseline), issues)
        try:
            for group, keys in (("mcp_servers", ("source", "scope", "name")), ("skills", ("path",))):
                if not isinstance(baseline[group], list):
                    raise ValueError()
                for item in baseline[group]:
                    if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in keys):
                        raise ValueError()
                    if "health" in item and not isinstance(item["health"], dict):
                        raise ValueError()
        except (TypeError, KeyError, ValueError):
            ap.error("--baseline must be an inspector report with valid mcp_servers and skills arrays")

    cfg_paths = discover_mcp_configs(claude_home, project_root, args.scope, claude_config, issues)
    cfg_paths = sorted(set(cfg_paths + [Path(p).expanduser().resolve() for p in args.config]), key=str)
    config_data = {p: load_json(p, issues) for p in cfg_paths}
    settings_paths = [claude_home / "settings.json", claude_home / "settings.local.json"]
    if args.scope in ("all", "project"):
        settings_paths.extend([project_root / ".claude/settings.json", project_root / ".claude/settings.local.json"])
    settings = []
    for path in settings_paths:
        if path not in config_data and path.exists():
            config_data[path] = load_json(path, issues)
        if config_data.get(path):
            settings.append(config_data[path])
    disabled: set[str] = set()
    rejected: set[str] = set()
    for data in settings:
        names = data.get("disabledMcpjsonServers", [])
        if isinstance(names, list):
            rejected.update(n for n in names if isinstance(n, str))
    data = config_data.get(claude_config) or {}
    entry = project_settings(data, project_root)
    if isinstance(entry, dict) and isinstance(entry.get("disabledMcpServers", []), list):
        disabled.update(n for n in entry.get("disabledMcpServers", []) if isinstance(n, str))
    active_plugins = active_plugin_roots(claude_home, project_root, settings, issues) if args.scope != "project" else set()
    for root in sorted(active_plugins, key=str):
        path = root / ".mcp.json"
        if path.exists() and path not in config_data:
            cfg_paths.append(path)
            config_data[path] = load_json(path, issues)
    servers = parse_mcp_servers(cfg_paths, disabled, project_root=project_root, scope=args.scope,
                                claude_config=claude_config, claude_home=claude_home,
                                issues=issues, config_data=config_data, active_plugins=active_plugins,
                                rejected_project=rejected)
    if args.server:
        missing = set(args.server) - {s.name for s in servers}
        if missing:
            ap.error("unknown --server names: " + ", ".join(sorted(missing)))
        servers = [s for s in servers if s.name in args.server]
    skills = discover_skills(claude_home, project_root, args.scope, issues)
    for server in servers:
        classify_server(server)
        server._protocol_era = args.protocol_era
    if args.probe:
        run_probes(servers, args.probe_timeout, args.workers)

    meta = {
        "schema_version": 2,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "scope": args.scope, "probe": args.probe, "workers": args.workers,
        "protocol_era": args.protocol_era,
        "claude_home": str(claude_home), "claude_config": str(claude_config),
        "project_root": str(project_root), "server_filter": args.server,
        "config_files_scanned": [str(p) for p in sorted(set(cfg_paths), key=str)],
        "duration_ms": round((time.monotonic() - start) * 1000, 1),
    }
    write_reports(out_dir, servers, skills, meta, issues, baseline)
    print(f"Wrote {out_dir / 'report.md'}")
    print(f"Wrote {out_dir / 'report.json'}")
    unhealthy = any(s.enabled and s.effective and s.health.get("status") != "ok" for s in servers)
    return int((args.strict and bool(issues)) or (args.fail_on_unhealthy and (unhealthy or bool(issues))))


if __name__ == "__main__":
    sys.exit(main())
