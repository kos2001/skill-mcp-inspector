# skill-mcp-inspector

<p><a href="README.md" lang="ko">한국어</a> · <strong lang="en">English</strong></p>

A tool that inventories the MCP servers and skills configured for Claude Code, and writes a report on their potential for external communication and their actual connection status. It runs on Python 3.10+ with the standard library only.

The default run only reads files. With `--probe` it launches the configured server processes or connects to HTTP endpoints to check their health.

## Features

- **Inventory:** scans user, project and local MCP configuration, skills, and the plugin cache.
- **Configuration diagnostics:** reports invalid JSON, missing environment variables and malformed server definitions.
- **Precedence:** distinguishes local > project > user configuration and excludes disabled or shadowed servers from health checks.
- **Communication classification:** classifies `local`, `external` and `unknown` and records the reasoning. For skills it also cites the file and line number of the evidence.
- **Protocol probes:** validates initialization and tool listing over stdio and Streamable HTTP, handling tool pagination and HTTP JSON/SSE responses.
- **Parallel probing:** bounds concurrency and per-server probe time, and reads stderr concurrently to avoid log-induced deadlocks.
- **Report diffing:** compares against a previous snapshot and shows added, removed and changed servers/skills.
- **Automation:** JSON/Markdown reports, failure exit codes and a GitHub Actions test configuration.

## Getting started

```bash
git clone https://github.com/kos2001/skill-mcp-inspector.git
cd skill-mcp-inspector
python3 scripts/audit.py --out ./mcp-inspector-out
```

No package installation is needed. Results are written to two files.

| File | Purpose |
|------|---------|
| `mcp-inspector-out/report.md` | human-readable summary, server status, classification reasoning, diagnostics |
| `mcp-inspector-out/report.json` | inventory, status, evidence and diff results for automation |

To inspect another project, point at its path.

```bash
python3 scripts/audit.py --scope project --project-root /path/to/project
```

### Install as a Claude Code skill

From the checked-out repository, copy the skill file and the resources it needs. An existing installation is updated in place.

```bash
mkdir -p ~/.claude/skills/mcp-skill-inspector
cp SKILL.md ~/.claude/skills/mcp-skill-inspector/
cp -R scripts references ~/.claude/skills/mcp-skill-inspector/
```

The skill directory name follows the `name: mcp-skill-inspector` frontmatter in `SKILL.md` (which differs from the repository name). Once installed, requests in Claude Code such as "check the health of my MCP servers" or "list the installed skills that talk to the network" select the skill; the procedure the agent follows is in [SKILL.md](SKILL.md). Tests and development run from the repository, which includes `tests/`.

### Sample output

`report.md` is organized as summary, server table, then skill classification (example of a run without probes).

```text
# MCP & Skill Inspector Report

- Scope: `project`  Probe: `False`
- Project root: /path/to/project

## Summary

- MCP servers: **2** (classification: {'local': 1, 'external': 1}, transport: {'stdio': 1, 'http': 1})
- Skills: **3** (classification: {'local': 2, 'external': 1})

## MCP Servers

| Name | Scope | Enabled | Effective | Transport | Class | Health | Latency | Source |
|------|-------|---------|-----------|-----------|-------|--------|---------|--------|
| ...  |       |         |           |           |       |        |         |        |

## Skills
### External skills (1)
### Skills with no static external markers (2)
```

## Usage examples

### Check the connection status of a project's servers

```bash
python3 scripts/audit.py \
  --scope project \
  --project-root /path/to/project \
  --probe --workers 4 --probe-timeout 8
```

`--probe` actually uses the configured commands and URLs. Plugin caches not confirmed as active installations and shadowed definitions are not executed. Probes read metadata and tool lists; they never invoke tools.

### Check specific servers or a separate configuration file

```bash
python3 scripts/audit.py --probe --server my-server
python3 scripts/audit.py --scope project --config /path/to/mcp.json --probe
```

`--server` and `--config` may be repeated. An unknown server name exits with an argument error.

### Check servers on the newer protocol

The default `legacy` mode uses `initialize` and `notifications/initialized` and negotiates a supported version from 2024-11-05 through 2025-11-25. For servers on the 2026-07-28 protocol, select `modern` mode.

```bash
python3 scripts/audit.py --probe --server my-server --protocol-era modern
```

`modern` mode uses `server/discover` and per-request metadata. There is no automatic switching between the two modes.

### Compare with a previous report

```bash
python3 scripts/audit.py --scope project --out ./mcp-inspector-out
cp ./mcp-inspector-out/report.json /tmp/mcp-inspector-baseline.json
python3 scripts/audit.py --scope project \
  --baseline /tmp/mcp-inspector-baseline.json --out ./mcp-inspector-out
```

Compare reports generated with the same project path, scope and server filter. Timestamp and latency changes are ignored, and health changes are compared only when both reports contain probe results. Credential values are omitted from reports, so credential rotation is not detected.

### Fail in CI

```bash
python3 scripts/audit.py --scope project --probe --strict --fail-on-unhealthy
```

| Exit code | Meaning |
|-----------|---------|
| `0` | the requested run and probes succeeded |
| `1` | diagnostics found with `--strict`, or a failed/unverified status or diagnostics with `--fail-on-unhealthy` |
| `2` | invalid arguments or baseline report |

`--fail-on-unhealthy` requires `--probe`. It fails when any enabled, effective server's status is not `ok`, including missing executables and auth-required states. The report is written before exiting with code `1`.

## Scope and how to read the report

| Scope | What is inspected |
|-------|-------------------|
| `all` | user and current-project configuration; the default |
| `user` | user MCP configuration, Claude skills, plugin cache and marketplaces |
| `project` | the project's local MCP entries, `.mcp.json`, `.claude/settings*.json`, `.claude/skills` |

User and local MCP entries are read from `~/.claude.json`; server definitions in legacy `~/.claude/settings*.json` are also supported. Disable policies in user settings may also apply to project servers. `enabled` means configured activation; `effective` marks the definition that wins among user, project and local definitions. Neither implies runtime approval or a successful connection.

### Communication classification

| Class | Meaning |
|-------|---------|
| `external` | static evidence found: a non-loopback URL, a known external service, a skill using HTTP/web tools, etc. |
| `local` | evidence of local functionality or loopback endpoints, or a completed skill scan found no external-communication markers |
| `unknown` | insufficient reliable evidence, invalid configuration, or an incomplete scan |

Classification is inference from source and configuration, not traffic measurement. `local` does not guarantee that external communication is impossible. See [classification and status rules](references/classification-rules.md) for the detailed rules.

### Probe status

| Status | Meaning |
|--------|---------|
| `ok` | valid initialization/discovery response and full verification of the tool list the server advertises |
| `partial` | initialization/discovery succeeded but the tool-list check failed |
| `auth_required` / `forbidden` | HTTP 401 / 403; not treated as a verified connection |
| `reachable` | a legacy HTTP+SSE endpoint responded, but the MCP initialization is unverified |
| `config_error` | malformed definition or missing required environment variable; not executed |
| `timeout` / `exited` / `unreachable` / `error` | timed out, process exited, connection failed, protocol or execution error |
| `disabled` / `skipped` | disabled, cached, shadowed definition, missing executable, or unsupported probe target |

An HTTP response alone is not treated as healthy. Servers that only provide resources/prompts are healthy if initialization/discovery succeeds. Detailed errors and masked stderr are recorded under `health` in the JSON.

## CLI options

| Option | Default or role |
|--------|-----------------|
| `--out DIR` | `./mcp-inspector-out` |
| `--scope all/user/project` | `all` |
| `--project-root PATH` | current working directory |
| `--claude-home PATH` | `CLAUDE_HOME` or `~/.claude` |
| `--claude-config PATH` | `.claude.json` in the same parent directory as the Claude home |
| `--config PATH` | additional MCP JSON file; repeatable |
| `--server NAME` | exact server-name filter; repeatable |
| `--probe` | enable real health probes |
| `--protocol-era legacy/modern` | `legacy` |
| `--probe-timeout SECONDS` | per-server protocol budget; default 8, positive and finite |
| `--workers N` | parallel probes; default 4, range 1–32; 1 runs sequentially |
| `--baseline PATH` | previous `report.json` to compare against |
| `--strict` | fail on scan/configuration diagnostics |
| `--fail-on-unhealthy` | fail when an enabled, effective server is not `ok` or diagnostics exist |

Full help: `python3 scripts/audit.py --help`

## Tests and performance

```bash
python3 -m unittest discover -s tests -v
python3 scripts/benchmark.py
```

Tests use temporary configuration trees and local fake stdio/HTTP servers. **26 tests pass** locally on Python 3.10.20 and 3.14.7. [GitHub Actions](.github/workflows/test.yml) is configured to test Linux and macOS with Python 3.10 and 3.14. Actions were disabled on the repository at the time of this change, so there are no remote CI results.

Local measurement with 8 fake stdio servers, 150 ms initialization delay per server, 3 repetitions.

| Run mode | Median run time |
|----------|-----------------|
| sequential, 1 worker | 1,573.7 ms |
| parallel, 4 workers | 392.6 ms |

The measurement showed a **4.01x speedup**, roughly **75% less time**. Results vary with real server latency and load. Measurement conditions and the official GitHub/MCP sources are collected in [improvement rationale and verification record](references/improvements.md).

## Limitations

- Stdio probes are supported on macOS and Linux. Legacy HTTP+SSE gets a reachability check only; WebSocket and SDK servers are inventoried but their probes are skipped.
- The configuration/cache inventory does not reproduce every runtime connection. Managed policies, MCP definitions in inline plugin manifests, dynamic auth headers, OAuth login, approval state and plugin endpoint deduplication are not handled.
- Probes are one-shot. DNS lookup, process creation and the short cleanup allowance may exceed the protocol budget, and disconnected SSE streams are not resumed.
- Dependency/build directories and nested directory symlinks are not scanned. Skill/script files are processed up to 2 MiB, protocol responses up to 1 MiB, and tool lists up to 100 pages.
- Reports omit runtime environment/header values and mask recognizable credentials. There is no guarantee that every secret in arbitrary server messages is found, so review the contents before sharing.

## Layout

```text
SKILL.md                         agent instructions
scripts/audit.py                 configuration/skill inventory, classification, report and CLI
scripts/probes.py                stdio/HTTP protocol probes
scripts/benchmark.py             local performance measurement
tests/                          regression/integration tests and fake servers
references/classification-rules.md  classification and status rules
references/improvements.md       research sources and verification results
.github/workflows/test.yml       CI test configuration
```

## License

[MIT License](LICENSE)
