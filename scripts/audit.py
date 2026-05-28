#!/usr/bin/env python3
"""mcp-skill-inspector — audit installed Skills and MCP servers.

Outputs a stability + external-dependency report in JSON and Markdown.

Usage:
    python inspect.py [--out DIR] [--probe] [--probe-timeout SECS]
                      [--claude-home PATH] [--scope SCOPE]

Scopes:
    all       enumerate user-level (~/.claude) and project-level configs (default)
    user      only ~/.claude/**
    project   only ./.mcp.json, ./.claude/settings*.json, ./.claude/skills/**

--probe actually spawns each stdio MCP server and runs initialize+tools/list,
and for http/sse servers performs a HEAD-style probe. Skipped by default —
it is safe but it does start subprocesses and make outbound requests.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
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
    (re.compile(r"\bcurl\s+-[a-zA-Z]*\s*['\"]?https?://", re.I), "shells out to curl"),
    (re.compile(r"\bwget\s+https?://", re.I), "shells out to wget"),
    (re.compile(r"requests\.(get|post|put|delete|head)\(", re.I), "uses python-requests"),
    (re.compile(r"\bfetch\(['\"]https?://", re.I), "uses fetch()"),
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


@dataclass
class Skill:
    name: str
    path: str
    scope: str            # "user" | "plugin" | "project"
    description: str = ""
    classification: str = "unknown"
    reasons: list[str] = field(default_factory=list)
    size_bytes: int = 0


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def discover_mcp_configs(claude_home: Path, project_root: Path, scope: str) -> list[Path]:
    paths: list[Path] = []
    if scope in ("all", "user"):
        for name in ("settings.json", "settings.local.json"):
            p = claude_home / name
            if p.exists():
                paths.append(p)
        # Plugin-bundled .mcp.json files (cache + marketplaces)
        for sub in ("plugins/cache", "plugins/marketplaces"):
            root = claude_home / sub
            if root.exists():
                paths.extend(root.rglob(".mcp.json"))
    if scope in ("all", "project"):
        for name in (".mcp.json", ".claude/settings.json", ".claude/settings.local.json"):
            p = project_root / name
            if p.exists():
                paths.append(p)
    return paths


def parse_mcp_servers(config_paths: list[Path], disabled: set[str]) -> list[McpServer]:
    servers: list[McpServer] = []
    seen: set[tuple[str, str]] = set()
    for cfg in config_paths:
        data = load_json(cfg)
        if not isinstance(data, dict):
            continue
        # settings.json shape: { mcpServers: {...} }
        # .mcp.json shape:    { <name>: {...} }  or { mcpServers: {...} }
        block = data.get("mcpServers") if "mcpServers" in data else data
        if not isinstance(block, dict):
            continue
        # Heuristic for raw .mcp.json without `mcpServers` key:
        # values must be dicts that look like server defs.
        for name, spec in block.items():
            if not isinstance(spec, dict):
                continue
            if not any(k in spec for k in ("command", "url", "type", "args")):
                continue
            key = (name, str(cfg))
            if key in seen:
                continue
            seen.add(key)
            transport = spec.get("type") or ("http" if spec.get("url") else "stdio")
            scope = (
                "plugin" if "plugins/" in str(cfg)
                else "project" if str(cfg).startswith(str(Path.cwd()))
                else "user"
            )
            servers.append(McpServer(
                name=name,
                source=str(cfg),
                scope=scope,
                transport=transport,
                command=spec.get("command"),
                args=list(spec.get("args") or []),
                url=spec.get("url"),
                env_keys=sorted((spec.get("env") or {}).keys()),
                enabled=name not in disabled,
            ))
    return servers


def discover_skills(claude_home: Path, project_root: Path, scope: str) -> list[Skill]:
    skills: list[Skill] = []
    roots: list[tuple[Path, str]] = []
    if scope in ("all", "user"):
        for sub in ("skills", "plugins/cache", "plugins/marketplaces"):
            p = claude_home / sub
            if p.exists():
                roots.append((p, "plugin" if "plugins" in sub else "user"))
    if scope in ("all", "project"):
        p = project_root / ".claude" / "skills"
        if p.exists():
            roots.append((p, "project"))

    for root, scope_label in roots:
        for skill_md in root.rglob("SKILL.md"):
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            name, description = parse_frontmatter(text)
            skills.append(Skill(
                name=name or skill_md.parent.name,
                path=str(skill_md),
                scope=scope_label,
                description=description,
                size_bytes=skill_md.stat().st_size,
            ))
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
            name = " ".join(buf).strip()
        elif current_key == "description":
            desc = " ".join(buf).strip()
        buf = []
    for line in block.splitlines():
        if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", line):
            flush()
            key, _, val = line.partition(":")
            current_key = key.strip()
            val = val.strip()
            if val:
                buf.append(val)
        else:
            buf.append(line.strip())
    flush()
    return name, desc


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_server(s: McpServer) -> None:
    reasons: list[str] = []
    score_external = 0
    score_local = 0
    if s.transport in ("http", "sse") or s.url:
        score_external += 3
        reasons.append(f"remote transport {s.transport!r} → {s.url}")
    blob = " ".join([s.command or "", *s.args]).lower()
    for hint in EXTERNAL_PACKAGE_HINTS:
        if hint in blob:
            score_external += 2
            reasons.append(f"args reference external service '{hint}'")
            break
    for hint in LOCAL_PACKAGE_HINTS:
        if hint in blob:
            score_local += 2
            reasons.append(f"args reference local capability '{hint}'")
            break
    if s.env_keys:
        if any(k.upper().endswith(("_KEY", "_TOKEN", "_SECRET")) for k in s.env_keys):
            score_external += 1
            reasons.append(f"requires credentials: {s.env_keys}")
    if s.command in (None, "") and s.url:
        score_external += 1
    if score_external > score_local:
        s.classification = "external"
    elif score_local > score_external:
        s.classification = "local"
    else:
        # default unknown stdio server → assume local but flag it
        s.classification = "local" if s.command else "unknown"
        if not reasons:
            reasons.append("no external markers; treated as local by default")
    s.reasons = reasons


def classify_skill(skill: Skill) -> None:
    reasons: list[str] = []
    try:
        text = Path(skill.path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        skill.classification = "unknown"
        skill.reasons = ["could not read SKILL.md"]
        return
    hay = text + " " + skill.description
    for pattern, label in EXTERNAL_SKILL_PATTERNS:
        if pattern.search(hay):
            reasons.append(label)
    # Also scan sibling scripts/ for hints
    scripts_dir = Path(skill.path).parent / "scripts"
    if scripts_dir.exists():
        for f in scripts_dir.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".sh", ".js", ".ts", ".mjs"):
                try:
                    sub = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for pattern, label in EXTERNAL_SKILL_PATTERNS:
                    if pattern.search(sub):
                        msg = f"{label} (in {f.name})"
                        if msg not in reasons:
                            reasons.append(msg)
    skill.classification = "external" if reasons else "local"
    skill.reasons = reasons[:6]


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------

def _expand_plugin_placeholders(value: str, source: str) -> str:
    """Replace ${CLAUDE_PLUGIN_ROOT} with the directory containing the .mcp.json."""
    if "${CLAUDE_PLUGIN_ROOT}" not in value:
        return value
    plugin_root = str(Path(source).parent)
    return value.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)


def probe_stdio(s: McpServer, timeout: float) -> dict[str, Any]:
    """Spawn the server, send initialize + tools/list, measure latency."""
    if not s.command or shutil.which(s.command) is None:
        return {"status": "skipped", "error": f"command not on PATH: {s.command}"}
    args = [_expand_plugin_placeholders(a, s.source) for a in s.args]
    cwd = Path(s.source).parent if "${CLAUDE_PLUGIN_ROOT}" in " ".join(s.args) else None
    t0 = time.perf_counter()
    try:
        proc = subprocess.Popen(
            [s.command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd) if cwd else None,
        )
    except Exception as e:
        return {"status": "error", "error": f"spawn failed: {e}"}

    def send(msg: dict) -> None:
        assert proc.stdin
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    import select as _select
    buf = {"data": ""}

    def recv_until(predicate, deadline) -> tuple[dict | None, str]:
        """Return (object, state) where state ∈ {match, exited, timeout}.

        Uses select() so a silent server cannot hang the probe past `deadline`.
        """
        assert proc.stdout
        fd = proc.stdout.fileno()
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None, "timeout"
            ready, _, _ = _select.select([fd], [], [], min(remaining, 0.5))
            if not ready:
                if proc.poll() is not None:
                    return None, "exited"
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return None, "exited"
            buf["data"] += chunk.decode("utf-8", errors="replace")
            while "\n" in buf["data"]:
                line, buf["data"] = buf["data"].split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if predicate(obj):
                    return obj, "match"

    result: dict[str, Any] = {"status": "unknown"}
    deadline = time.perf_counter() + timeout
    try:
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-skill-inspector", "version": "0.1"},
            },
        })
        init, state = recv_until(lambda o: o.get("id") == 1, deadline)
        if init is None:
            result["status"] = "exited" if state == "exited" else "timeout"
            result["error"] = "process exited before responding" if state == "exited" else "no initialize response"
        else:
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools, state = recv_until(lambda o: o.get("id") == 2, deadline)
            if tools is None:
                result["status"] = "partial"
                result["error"] = "tools/list " + ("crashed" if state == "exited" else "timed out")
                result["server_info"] = init.get("result", {}).get("serverInfo")
            else:
                tool_list = (tools.get("result") or {}).get("tools") or []
                result["status"] = "ok"
                result["tool_count"] = len(tool_list)
                result["tool_names"] = [t.get("name") for t in tool_list][:30]
                result["server_info"] = init.get("result", {}).get("serverInfo")
    finally:
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try: proc.kill()
            except Exception: pass
            try: proc.wait(timeout=2)
            except Exception: pass
        # capture a small slice of stderr for diagnostics — non-blocking
        err = ""
        if proc.stderr:
            try:
                fd = proc.stderr.fileno()
                import fcntl as _fcntl
                fl = _fcntl.fcntl(fd, _fcntl.F_GETFL)
                _fcntl.fcntl(fd, _fcntl.F_SETFL, fl | os.O_NONBLOCK)
                chunks = []
                for _ in range(20):
                    try:
                        c = os.read(fd, 65536)
                        if not c:
                            break
                        chunks.append(c.decode("utf-8", errors="replace"))
                    except BlockingIOError:
                        break
                err = "".join(chunks)
            except Exception:
                err = ""
        if err:
            result["stderr_tail"] = err[-400:]
    return result


def probe_http(s: McpServer, timeout: float) -> dict[str, Any]:
    if not s.url:
        return {"status": "skipped", "error": "no url"}
    t0 = time.perf_counter()
    parsed = urllib.parse.urlparse(s.url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    result: dict[str, Any] = {"url": s.url, "host": host}
    # TCP reachability first
    try:
        with socket.create_connection((host, port), timeout=timeout):
            result["tcp"] = "reachable"
    except Exception as e:
        result["status"] = "unreachable"
        result["error"] = f"tcp: {e}"
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result
    # HTTP probe — many MCP HTTP endpoints reject GET; accept any HTTP response as "alive".
    try:
        req = urllib.request.Request(s.url, method="GET",
                                     headers={"User-Agent": "mcp-skill-inspector/0.1",
                                              "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result["http_status"] = resp.status
            result["status"] = "ok"
    except urllib.error.HTTPError as e:
        result["http_status"] = e.code
        # 4xx still means the server is responding
        result["status"] = "ok" if 400 <= e.code < 500 else "error"
        result["error"] = f"http {e.code}"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


def probe_server(s: McpServer, timeout: float) -> None:
    if not s.enabled:
        s.health = {"status": "disabled"}
        return
    if s.transport in ("http", "sse") or s.url:
        s.health = probe_http(s, timeout)
    else:
        s.health = probe_stdio(s, timeout)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_reports(out_dir: Path, servers: list[McpServer], skills: list[Skill],
                  meta: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "mcp_servers": [asdict(s) for s in servers],
        "skills": [asdict(s) for s in skills],
        "summary": summarize(servers, skills),
    }
    (out_dir / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(payload), encoding="utf-8")


def summarize(servers: list[McpServer], skills: list[Skill]) -> dict[str, Any]:
    def by(field_, items):
        out: dict[str, int] = {}
        for it in items:
            out[getattr(it, field_)] = out.get(getattr(it, field_), 0) + 1
        return out
    healthy = sum(1 for s in servers if s.health.get("status") == "ok")
    probed = sum(1 for s in servers if s.health)
    return {
        "server_count": len(servers),
        "server_classification": by("classification", servers),
        "server_transport": by("transport", servers),
        "servers_probed": probed,
        "servers_ok": healthy,
        "skill_count": len(skills),
        "skill_classification": by("classification", skills),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    summary = payload["summary"]
    lines: list[str] = []
    lines.append("# MCP & Skill Inspector Report")
    lines.append("")
    lines.append(f"- Generated: {meta['generated_at']}")
    lines.append(f"- Scope: `{meta['scope']}`  Probe: `{meta['probe']}`")
    lines.append(f"- Claude home: `{meta['claude_home']}`")
    lines.append(f"- Project root: `{meta['project_root']}`")
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
    lines.append("## MCP Servers")
    lines.append("")
    lines.append("| Name | Scope | Transport | Class | Health | Latency | Source |")
    lines.append("|------|-------|-----------|-------|--------|---------|--------|")
    for s in payload["mcp_servers"]:
        h = s.get("health") or {}
        status = h.get("status", "-")
        lat = f"{h.get('latency_ms','-')} ms" if h.get("latency_ms") is not None else "-"
        lines.append(f"| `{s['name']}` | {s['scope']} | {s['transport']} | "
                     f"**{s['classification']}** | {status} | {lat} | "
                     f"`{Path(s['source']).name}` |")
    lines.append("")
    ext_servers = [s for s in payload["mcp_servers"] if s["classification"] == "external"]
    if ext_servers:
        lines.append("### External MCP servers (touch the network)")
        for s in ext_servers:
            target = s["url"] or " ".join([s["command"] or "", *s["args"]])
            lines.append(f"- **{s['name']}** → `{target}`")
            for r in s["reasons"]:
                lines.append(f"  - {r}")
            if s.get("env_keys"):
                lines.append(f"  - env: {s['env_keys']}")
        lines.append("")
    failed = [s for s in payload["mcp_servers"]
              if (s.get("health") or {}).get("status") not in (None, "ok", "disabled", "skipped")]
    if failed:
        lines.append("### Unhealthy servers")
        for s in failed:
            h = s["health"]
            lines.append(f"- **{s['name']}** — {h.get('status')}: {h.get('error','')}")
        lines.append("")
    lines.append("## Skills")
    lines.append("")
    lines.append(f"Total: {len(payload['skills'])}")
    lines.append("")
    ext_skills = [s for s in payload["skills"] if s["classification"] == "external"]
    lines.append(f"### External skills ({len(ext_skills)})")
    lines.append("")
    for s in sorted(ext_skills, key=lambda x: x["name"]):
        lines.append(f"- **{s['name']}** (`{s['scope']}`) — {', '.join(s['reasons'])}")
        lines.append(f"  - `{s['path']}`")
    lines.append("")
    local_skills = [s for s in payload["skills"] if s["classification"] == "local"]
    lines.append(f"### Local-only skills ({len(local_skills)})")
    lines.append("")
    for s in sorted(local_skills, key=lambda x: x["name"])[:50]:
        lines.append(f"- {s['name']} (`{s['scope']}`)")
    if len(local_skills) > 50:
        lines.append(f"- … and {len(local_skills) - 50} more (see report.json)")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Inspect installed Skills and MCP servers")
    ap.add_argument("--out", default="./mcp-inspector-out",
                    help="output directory (default: ./mcp-inspector-out)")
    ap.add_argument("--probe", action="store_true",
                    help="actually spawn stdio servers and hit http endpoints")
    ap.add_argument("--probe-timeout", type=float, default=8.0,
                    help="per-server probe timeout in seconds (default: 8)")
    ap.add_argument("--claude-home", default=os.environ.get("CLAUDE_HOME",
                                                            str(Path.home() / ".claude")))
    ap.add_argument("--scope", choices=("all", "user", "project"), default="all")
    args = ap.parse_args(argv)

    claude_home = Path(args.claude_home).expanduser().resolve()
    project_root = Path.cwd().resolve()
    out_dir = Path(args.out).expanduser().resolve()

    # Disabled MCP servers from settings.json
    disabled: set[str] = set()
    for f in (claude_home / "settings.json", claude_home / "settings.local.json"):
        d = load_json(f) or {}
        for k in d.get("disabledMcpjsonServers", []) or []:
            disabled.add(k)

    cfg_paths = discover_mcp_configs(claude_home, project_root, args.scope)
    servers = parse_mcp_servers(cfg_paths, disabled)
    skills = discover_skills(claude_home, project_root, args.scope)

    for s in servers:
        classify_server(s)
    for sk in skills:
        classify_skill(sk)

    if args.probe:
        for s in servers:
            if s.enabled:
                probe_server(s, args.probe_timeout)

    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "scope": args.scope,
        "probe": args.probe,
        "claude_home": str(claude_home),
        "project_root": str(project_root),
        "config_files_scanned": [str(p) for p in cfg_paths],
    }
    write_reports(out_dir, servers, skills, meta)
    print(f"Wrote {out_dir / 'report.md'}")
    print(f"Wrote {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
