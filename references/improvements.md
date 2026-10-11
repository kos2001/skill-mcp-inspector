# Research and verification

Reviewed on 2026-10-11. Primary sources informed the changes below; this reference
records implementation decisions rather than copying upstream documentation.

| Primary source | Applied change |
|----------------|----------------|
| [Official MCP Inspector CLI on GitHub](https://github.com/modelcontextprotocol/inspector/blob/main/clients/cli/README.md) | explicit targets, bounded timeouts, tool enumeration, and useful CI failure states |
| [MCP 2025-11-25 lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) | validate initialize, negotiate supported versions/capabilities, send initialized, respect unsupported versions |
| [MCP 2025-11-25 transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) | Streamable HTTP POST, JSON/SSE replies, session/version headers, shutdown; separate HTTP reachability from MCP success |
| [MCP 2026-07-28 versioning](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning) and [discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover) | optional modern mode, discovery and request metadata, without silently mixing protocol eras |
| [MCP 2026-07-28 Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) | modern protocol/method headers alongside matching request metadata |
| [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp) | `.claude.json` user/local scopes, local > project > user precedence, variable/default expansion, project disable settings |
| [Official GitHub checkout](https://github.com/actions/checkout) and [setup-python](https://github.com/actions/setup-python) | Python 3.10/3.14 test matrix on macOS/Linux |

## Improvements delivered

- Configurations are parsed once per run; unreadable, malformed, and invalid
  definitions produce diagnostics. Invalid server definitions remain in inventory.
- Known cache entries are separated from registered active plugin installations;
  disabled or shadowed definitions do not launch probes.
- Stdio probes drain stdout/stderr together, preserve buffered messages, bound
  response sizes, and terminate the process group. Silent or noisy servers cannot
  stall subsequent probes indefinitely under normal local process operation.
- Probes run with bounded concurrency, using one timeout budget across protocol
  requests. Output order follows inventory order rather than completion order.
- Static hints use deterministic token boundaries, unknown servers remain
  unknown, and skill evidence records file/line locations without source snippets.
- Reports redact recognizable credentials, escape table content, and replace each
  report file atomically. Baseline comparisons ignore timing noise.
- CI flags fail on degraded/unverified checks or incomplete scans. Automated tests
  use temporary configuration trees and local servers, without production probes.

## Measured performance

Command: `python3 scripts/benchmark.py`

Local macOS, Python 3.14.7; eight synthetic stdio servers, each with a 150 ms
initialization delay, three repetitions, one advertised tool per server. All
probes passed and counted one tool in both execution modes.

| Mode | Median elapsed time |
|------|---------------------|
| One worker | 1573.7 ms |
| Four workers | 392.6 ms |

Measured speedup: **4.01×**, or approximately **75% less elapsed time**. Sequential
samples: 1510.9, 1575.7, 1573.7 ms. Parallel samples: 398.0, 387.9, 392.6 ms.
This measures the effect of concurrency on synthetic latency; it is not a
prediction for real services or a throughput/load test. The benchmark is
reproducible and validates health and tool counts before printing results.

## Verification scope

`python3 -m unittest discover -s tests -v` exercises configuration scopes,
precedence, disable settings, plugin activation, invalid configuration diagnostics,
environment expansion and redaction, skill evidence and scan limits, snapshot
comparison, CI exits, classification, stdio errors/buffering/timeouts, HTTP
JSON/SSE/session/auth/errors, and both legacy/modern protocols.

Final local validation: **26 tests passed on both Python 3.10.20 and 3.14.7**.
The skill frontmatter validator passed. A separate CLI smoke run verified
initialization, paginated tool counts, self-scan evidence, JSON/Markdown output,
and a successful strict CI exit.

The GitHub matrix is configured for Ubuntu/macOS and Python 3.10/3.14.
At publication, the repository's GitHub Actions setting was disabled, so no
hosted CI run was produced; the validation results above are local results.
Legacy HTTP+SSE remains a reachability-only check. OAuth, dynamic authentication
helpers, traffic monitoring, and long-running stability measurements are outside
this implementation's scope.
