# Classification rules

Every label produced by `audit.py` is backed by one of the rules below. When the
user asks "why is X marked external?", quote the matching rule from this file
together with the `reasons` array in `report.json`.

## MCP servers

A server gets a score along two axes; the higher score wins. Ties default to
`local` for stdio servers, `unknown` if there is no command at all.

| Signal | Direction | Weight | Notes |
|--------|-----------|--------|-------|
| `type` is `http` or `sse`, or a `url` is set | external | +3 | Definitive — the transport itself is remote. |
| Args/command contain a known external-service substring | external | +2 | Substrings: `linear`, `github`, `gitlab`, `slack`, `notion`, `asana`, `jira`, `confluence`, `atlassian`, `zendesk`, `intercom`, `hubspot`, `salesforce`, `stripe`, `shopify`, `discord`, `telegram`, `gmail`, `google`, `calendar`, `drive`, `sheets`, `openai`, `anthropic`, `tavily`, `brave`, `exa`, `perplexity`, `firecrawl`, `upstash`, `context7`, `supabase`, `vercel`, `cloudflare`, `aws`, `azure`, `gcp`, `sentry`, `datadog`, `grafana`, `pagerduty`, `twilio`, `sendgrid`, `mailgun`. |
| Env block contains a `*_KEY`, `*_TOKEN`, or `*_SECRET` key | external | +1 | Credential shape implies an auth'd remote API. |
| Args/command contain a known local-capability substring | local | +2 | Substrings: `filesystem`, `sqlite`, `memory`, `fetch-local`, `shell`, `everything`, `git-local`. |
| Stdio command present but no other markers | local (default) | — | Reported with reason `no external markers; treated as local by default`. |

The hint lists live at the top of `scripts/audit.py` and are easy to extend.

## Skills

A skill is `external` if any of the patterns below match anywhere in `SKILL.md`
or in files under a sibling `scripts/` directory. Otherwise it is `local`.

| Pattern | Reason recorded |
|---------|-----------------|
| `WebFetch` | uses WebFetch |
| `WebSearch` | uses WebSearch |
| `mcp__<server>__<tool>` | calls MCP tool |
| `curl -... 'http(s)://...'` | shells out to curl |
| `wget http(s)://...` | shells out to wget |
| `requests.(get\|post\|put\|delete\|head)(...)` | uses python-requests |
| `fetch('http(s)://...')` | uses fetch() |
| `api[_-]?key` (case-insensitive) | references an API key |
| `gh (pr\|issue\|api\|repo) ...` | shells out to gh CLI |

These patterns intentionally err toward calling something external. A skill that
only *documents* an external integration but never calls it will still be flagged
— that's the safer mistake.

## Health probe states

| State | Meaning |
|-------|---------|
| `ok` | Server responded to `tools/list` (or HTTP server returned any response). |
| `partial` | `initialize` succeeded but `tools/list` did not (server is up but degraded). |
| `timeout` | No response within `--probe-timeout`; process still running. |
| `exited` | Process exited before responding. Check `stderr_tail` — usually a missing binary, missing env var, or wrong working directory. |
| `unreachable` | TCP connect failed (HTTP servers). DNS or firewall. |
| `error` | Other failure; see `error` field. |
| `skipped` | Command not on PATH, or server is disabled in settings. |
| `disabled` | Listed in `disabledMcpjsonServers`. |

An HTTP `401`/`403`/`405` is reported as `ok` — the server is alive and
talking the protocol, it just rejects unauthenticated requests.
