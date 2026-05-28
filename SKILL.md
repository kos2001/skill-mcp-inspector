---
name: mcp-skill-inspector
description: Audit installed MCP servers and Skills for stability and external-network exposure. Use whenever the user wants to know which MCP servers/Skills are configured, which ones reach external SaaS endpoints, which are healthy, or wants a stability/dependency report — including phrasings like "check my MCP servers", "are any of my skills calling the internet", "which servers are external", "MCP health check", "audit my plugins", or any request to verify what is installed versus what is actually working.
---

# MCP & Skill Inspector

## What this skill does

Produces a per-machine audit answering three questions:

1. **What is installed?** — every Skill (`SKILL.md`) and every MCP server declared in `~/.claude/settings*.json`, plugin-bundled `.mcp.json` files, and the project's `.mcp.json` / `.claude/settings*.json`.
2. **What touches the network?** — each item is classified `local` / `external` / `unknown` from concrete signals (remote URL, npm package matching a known SaaS, credentials in `env`, `WebFetch`/`WebSearch`/`curl` in skill content). Reasons are reported alongside the label so the user can verify.
3. **What actually works?** — when `--probe` is set, the script connects to each MCP server, runs `initialize` + `tools/list`, and records latency, tool count, and any stderr from crashes.

Output: `report.md` (human) and `report.json` (machine) in the chosen output directory.

## When to use

Trigger this skill whenever the user asks about MCP server inventory, MCP health, skill dependencies, internal vs external network use, plugin auditing, or compliance-style "what is reaching outside our network" questions. It is also the right tool when an MCP server seems broken and the user wants a structured diagnostic.

Don't trigger it for one-off "what does this tool do" questions — those are answered directly from the tool's docs.

## Workflow

1. **Run the audit (enumeration only)** — fast, no side effects, safe to run any time:
   ```bash
   python3 scripts/audit.py --out ./mcp-inspector-out
   ```
   Read `mcp-inspector-out/report.md` and summarize the three sections (summary, MCP table, external skills) for the user.

2. **Run with health probe** when the user asks "is it actually working" or wants stability data:
   ```bash
   python3 scripts/audit.py --out ./mcp-inspector-out --probe --probe-timeout 8
   ```
   This spawns each stdio MCP server briefly and makes one HTTP request per remote server. It is safe but not free — warn the user before probing in restricted environments. Use `--scope project` to limit probing to the current project.

3. **Read the report aloud** focusing on:
   - The external/local breakdown
   - Any servers in `Unhealthy servers` (status `exited`, `timeout`, `unreachable`, `error`)
   - External skills that may surprise the user (e.g., a skill they thought was offline)

4. **Follow-up actions to offer:**
   - For each `exited` stdio server, suggest reading `stderr_tail` in `report.json` — most failures are missing env vars, missing binaries (`php`, `docker`), or unexpanded `${CLAUDE_PLUGIN_ROOT}`.
   - For each `external` server, offer to look up the privacy/data-handling implications.
   - For each external skill, offer to show the matching pattern (the line that triggered the classification) so the user can audit it.

## Flags

| Flag | Effect |
|------|--------|
| `--scope all` (default) | scan `~/.claude` and project |
| `--scope user` | only `~/.claude` |
| `--scope project` | only the current project |
| `--probe` | actually connect to MCP servers and measure |
| `--probe-timeout SECS` | per-server timeout (default 8s) |
| `--out DIR` | output directory (default `./mcp-inspector-out`) |
| `--claude-home PATH` | override `~/.claude` (also reads `CLAUDE_HOME` env) |

## Classification cheat sheet

See `references/classification-rules.md` for the full rule list. Quick summary:

- **MCP external** — `type: http`/`sse`, has `url`, or args contain a known SaaS package name (linear, github, slack, notion, …), or has credential-shaped env keys (`*_KEY`, `*_TOKEN`, `*_SECRET`).
- **MCP local** — stdio server whose args match local capabilities (filesystem, sqlite, memory, …) or has no external markers.
- **Skill external** — SKILL.md or sibling scripts use `WebFetch`, `WebSearch`, `curl`/`wget`, `requests.*`, `fetch('http…')`, the `gh` CLI, an `mcp__*` tool call, or mention an API key.
- **Skill local** — none of the above.

The classifier is deliberately conservative: when a stdio server has no clear local markers it is labelled `local` with a `no external markers; treated as local by default` reason, so anything labelled `external` always has a concrete trigger you can quote.

## Known limitations

- **Plugin-bundled servers with `${CLAUDE_PLUGIN_ROOT}`** — the script expands this placeholder to the directory containing the `.mcp.json` when probing, which matches how Claude Code resolves it. If a plugin still fails with `ENOENT` after this, the plugin's manifest expects a different working directory.
- **HTTP probes do not authenticate** — a `401`/`403` response counts as "server reachable and responding". The script doesn't ship credentials around.
- **Static skill classification** — we infer "external" from SKILL.md content; a skill that conditionally uses the network only on some inputs may be over-classified. That's the safer error.
- **No long-running stability test** — `--probe` is a one-shot smoke test. For continuous monitoring, wire `report.json` into your own dashboard.

## Files

- `scripts/audit.py` — the inspector. Single file, stdlib only, Python 3.10+.
- `references/classification-rules.md` — detailed rule reference.
