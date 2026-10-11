# Classification and probe rules

Every classification has a `reasons` array. Skills also have `evidence` records
with `path`, `line`, and `reason`; snippets are omitted to avoid copying secrets.
Signals describe potential dependency, not observed network traffic.

## MCP classification

Rules apply in this order:

| Signal | Classification | Interpretation |
|--------|----------------|----------------|
| URL host outside literal loopback/`localhost` | external | network endpoint, including private LAN hosts |
| Command/args contain a known external-service hint | external | potential external SaaS dependency; takes precedence over local hints |
| Credential-shaped env keys without stronger external evidence | unknown | credentials can authenticate local services too |
| Known local-capability hint or loopback URL, without the above | local | local capability/endpoint evidence; server may still make outbound calls |
| No reliable signal or invalid configuration | unknown | no assumption of offline behavior |

Hint matches use alphanumeric boundaries, so `exa` does not match `example` and
`drive` does not match `driver`. Hints are sorted to make reasons deterministic.
The complete, editable sets are `EXTERNAL_PACKAGE_HINTS` and
`LOCAL_PACKAGE_HINTS` in `scripts/audit.py`.

A localhost address is classified without DNS lookup. Other hosts, even if they
resolve to localhost, are external static endpoints. Env key names containing
`token`, `secret`, `password`, `passwd`, `api_key`, `authorization`, or
`credential`, or ending in `_KEY`, are credential-shaped. Values are not exported.

## Skill classification

The scanner reads `SKILL.md` once and checks text and sibling scripts with
extensions `.py`, `.sh`, `.js`, `.ts`, `.mjs`, `.cjs`, and `.tsx`.

| Static pattern | Reason |
|----------------|--------|
| `WebFetch`, `WebSearch` | uses named web tool |
| `mcp__<server>__<tool>` | calls MCP tool; external potential even for a local MCP tool |
| `curl` / `wget` followed on the same line by an HTTP URL | shells out to HTTP client |
| `requests` / `httpx` request methods | uses Python HTTP client |
| `urlopen(...)` / `urlretrieve(...)` / `urllib.request.build_opener(...)` | uses urllib HTTP client |
| `fetch(...)` | uses fetch(); URL may be a variable |
| `api_key`, `api-key`, `api.key` | references an API key |
| `gh pr`, `gh issue`, `gh api`, `gh repo` | shells out to gh CLI |

Any match produces `external`, including documentation of optional network use.
Without a match, an incomplete file scan produces `unknown`; a completed scan
produces `local`, meaning only that no static external marker was found.
Patterns cannot detect every alias, dynamic import, referenced document, binary,
or network call. Skipped dependency/build directories and nested directory
symlinks are outside scan coverage. These exclusions are performance choices,
not evidence of offline behavior.

## Health states

| State | Meaning |
|-------|---------|
| `ok` | valid initialization/discovery and all tool pages, if tools are advertised |
| `partial` | initialization/discovery succeeded; tool listing errored, timed out, disconnected, or exceeded bounds |
| `auth_required` | HTTP 401; endpoint responds but access is unverified |
| `forbidden` | HTTP 403; endpoint responds but access is denied |
| `reachable` | legacy SSE returned HTTP success; no MCP handshake validation |
| `config_error` | invalid definition or unresolved required environment variable; no execution |
| `error` | invalid protocol/HTTP response, unsupported version, JSON-RPC error, or spawn error |
| `timeout` | initialization/discovery did not complete within the protocol budget |
| `exited` | stdio stdout closed before initialization/discovery response |
| `unreachable` | HTTP connection/DNS failure |
| `disabled` | configuration disabled, or plugin cache not confirmed as active installation |
| `skipped` | shadowed definition, missing executable, or unsupported probe transport/platform |

HTTP 404/405/500 and arbitrary HTML responses never count as `ok`. JSON-RPC
errors also cannot pass. The probe records capabilities and negotiated version;
only advertised tools are listed. Tool counts include all pages while tool names
are capped at 30. Repeated cursors and more than 100 pages produce `partial`.

The stdio reader drains stderr concurrently with stdout so logging cannot fill a
pipe and block initialization. It retains buffered messages across requests and
answers server `ping` requests. Cleanup closes stdin, then terminates/kills the
isolated process group. Report writes replace each destination atomically; the
JSON and Markdown pair is not a multi-file transaction.
