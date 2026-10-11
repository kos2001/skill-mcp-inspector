---
name: mcp-skill-inspector
description: Inventory Claude Code MCP configurations and installed or cached Skills, explain static network exposure, and optionally check MCP health. Use for MCP health checks, plugin inventories, external dependency audits, or diagnosing configured servers that do not work.
---

# MCP & Skill Inspector

Run the stdlib-only inspector with Python 3.10+. It produces `report.md` and
`report.json`, including configuration diagnostics, classification reasons, and
skill evidence with file paths and line numbers.

## Workflow

Start with an offline inventory:

```bash
python3 scripts/audit.py --out ./mcp-inspector-out
```

Discovery covers user and current-project entries in `~/.claude.json`, legacy
`~/.claude/settings*.json`, project `.mcp.json` and `.claude/settings*.json`,
Claude skill directories, and plugin cache/marketplace directories. The inventory
retains shadowed definitions and cached plugins. `enabled` describes configured
activation; `effective` identifies the winning definition among user, project,
and local scopes. These fields do not prove approval or runtime health.

When the user requests health checks, run a probe of the relevant servers:

```bash
python3 scripts/audit.py --scope project --probe --workers 4 --probe-timeout 8
```

Probes execute configured commands and contact configured URLs. Use the user's
requested scope; `--server NAME` selects particular servers. Enumeration alone
does neither. Cached plugin servers are disabled unless their installation path
is present in `plugins/installed_plugins.json` for the applicable scope and the
plugin is not disabled in settings. Probes skip disabled and shadowed definitions.

The default `legacy` protocol mode negotiates a version through `initialize`,
sends `notifications/initialized`, and lists all tool pages when the server
advertises tools. It supports revisions 2024-11-05, 2025-03-26, 2025-06-18, and
2025-11-25. Resources/prompts-only servers can also pass initialization.
Streamable HTTP supports JSON and SSE responses, configured headers, session
IDs, and best-effort session deletion. It refuses redirects.

For servers using the 2026-07-28 protocol, select modern discovery explicitly:

```bash
python3 scripts/audit.py --probe --server NAME --protocol-era modern
```

Modern mode uses `server/discover` and per-request metadata rather than an
initialization handshake. There is no automatic era fallback. Both modes inspect
metadata and tool lists; they never invoke tools.

Summarize the report using these distinctions:

- `ok`: valid MCP initialization/discovery and, if advertised, tool enumeration.
- `auth_required` / `forbidden`: endpoint responds, but access is not verified.
- `reachable`: legacy HTTP+SSE endpoint responds; its MCP handshake is unverified.
- `partial`: initialization/discovery worked but tool enumeration failed.
- `config_error`, `error`, `timeout`, `exited`, `unreachable`: inspect diagnostics
  and health details, including the redacted `stderr_tail`.
- `disabled` / `skipped`: not checked; neither counts as a successful probe.

Do not describe a `local` classification as proof that a server or skill cannot
contact the network. Classification is static evidence, not traffic monitoring.
Unrecognized servers and incomplete skill scans are `unknown`. Read
[references/classification-rules.md](references/classification-rules.md) when
explaining classification or health states.

## Comparison and automation

Compare two inventories taken with the same root, scope, and server filter:

```bash
python3 scripts/audit.py --baseline /path/to/previous/report.json --out ./current-report
```

The `changes` section lists added, removed, and changed servers/skills. It ignores
latency and timestamps; health status changes appear only when both snapshots
contain health results. Credential values are omitted, so credential rotations
are not detected. Save the baseline separately before reusing its output directory.

For CI, reports are written before returning a failure code:

```bash
python3 scripts/audit.py --scope project --probe --strict --fail-on-unhealthy
```

`--strict` returns 1 on scan/configuration diagnostics. `--fail-on-unhealthy`
requires `--probe` and returns 1 on diagnostics or any enabled, effective server
whose probe is not `ok`, including missing executables and unverified endpoints.
Invalid arguments or baselines return 2. A successful run returns 0.

## Options

| Flag | Effect |
|------|--------|
| `--scope all\|user\|project` | inventory scope; default `all` |
| `--project-root PATH` | project directory; default current working directory |
| `--claude-home PATH` | skill/settings/plugin directory; default `CLAUDE_HOME` or `~/.claude` |
| `--claude-config PATH` | global MCP config; default `.claude.json` beside Claude home |
| `--config PATH` | additional MCP JSON file; repeatable |
| `--server NAME` | exact server-name filter; repeatable |
| `--probe` | execute health probes |
| `--protocol-era legacy\|modern` | protocol mode; default `legacy` |
| `--probe-timeout SECONDS` | positive, finite per-server protocol budget; default 8 |
| `--workers N` | simultaneous probes, 1–32; default 4; use 1 to serialize |
| `--baseline PATH` | previous inspector JSON report |
| `--strict` | fail on diagnostics |
| `--fail-on-unhealthy` | fail on unsuccessful/unverified enabled probes or diagnostics |
| `--out DIR` | output directory; default `./mcp-inspector-out` |

## Practical limits

- Enumeration identifies configured/cached artifacts, not every runtime connector.
  Managed policies, inline plugin-manifest MCP definitions, dynamic headers
  helpers, OAuth login, and runtime approval decisions are not resolved.
- Plugin activation is inferred from the installation registry and settings.
  Plugin endpoint deduplication against other servers is not emulated.
- Legacy HTTP+SSE receives a reachability check only. WebSocket and SDK entries
  are inventoried but their probes are skipped. Stdio probes require macOS/Linux.
- One-shot probes do not establish long-term stability. The timeout covers
  protocol work, with a short process-cleanup allowance; OS DNS resolution or
  process creation can exceed it. Disconnected SSE streams are not resumed.
- Scans skip `.git`, `node_modules`, virtual environments, build output, and
  nested directory symlinks. Individual skill/script files over 2 MiB produce
  diagnostics. Protocol responses have a 1 MiB limit; tools have a 100-page limit.
- Reports omit runtime env/header values and redact known configured credentials,
  credential arguments, URL userinfo/query values, and common token forms.
  This is best-effort redaction of recognizable secrets, not a guarantee for
  arbitrary server messages. Review reports before sharing them.

## Maintenance

```bash
python3 -m unittest discover -s tests -v
python3 scripts/benchmark.py
```

Tests use temporary configuration trees and local fake stdio/HTTP servers.
The benchmark measures sequential versus parallel probes of local synthetic
servers. Read [references/improvements.md](references/improvements.md) for the
GitHub/specification sources behind the implementation and measured results.
