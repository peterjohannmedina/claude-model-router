# claude-model-router

An MCP adapter that lets Claude Code and Codex delegate **bounded semantic
work** to a trusted local model served over LiteLLM, keeping final judgment on
the frontier agent.

The worker exposes three read-only tools — semantic extraction, classification,
and first-pass diff review. It has no file-write and no shell access.

## Requirements

- Node.js 20+
- A LiteLLM endpoint serving the target model (default: `http://192.168.1.214:4000/v1`, model `muse`)

## Install

```bash
npm install
```

## Run

```bash
npm run mcp:local-inference
```

The default transport is stdio. Configure the route with `LITELLM_BASE_URL`
and `LITELLM_MODEL`.

## Claude Code integration

`.mcp.json` registers the stdio server, and `.claude/` wires up the delegation
route:

- `.claude/agents/local-muse-worker.md` — a read-only subagent limited to the
  three local tools, escalating final judgment to the parent agent.
- `.claude/settings.json` — `SessionStart` / `PreToolUse` / `SubagentStart`
  availability checks, plus scoped MCP permissions.

Before delegating, the hooks preflight the route with
`scripts/verify-local-inference-claude-resource.js`, which confirms
reachability **without generating inference**. If the local resource is
unreachable, Claude declines the spawn and answers on the frontier path rather
than simulating a local result.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LITELLM_BASE_URL` | `http://192.168.1.214:4000/v1` | OpenAI-compatible endpoint |
| `LITELLM_MODEL` | `muse` | Model route to call |
| `LOCAL_WORKER_MAX_OUTPUT_TOKENS` | `4000` | Output budget; must be ≥ 2000 |
| `LOCAL_WORKER_TIMEOUT_MS` | `60000` | Per-request timeout |
| `LOCAL_WORKER_TEMPERATURE` | `0` | Sampling temperature |

> **Do not lower the output budget below 2000.** Reasoning models spend
> `max_tokens` on hidden reasoning before emitting an answer, so a small budget
> returns empty content with `finish_reason=length` on real workloads. Both
> verifiers reject a budget under 2000 so this cannot regress silently.

See [docs/local-inference-mcp.md](docs/local-inference-mcp.md) for Codex,
Claude Code, and Streamable HTTP configuration.

## Tests

```bash
npm test
```
