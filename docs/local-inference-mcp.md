# Local inference MCP worker

This repository includes a narrow MCP adapter for the trusted local model
served by LiteLLM. It is intentionally separate from the existing Obsidian
and legacy SSE bridges.

The adapter exposes read-only tools to Codex and Claude Code:

- `local_extract` — semantic extraction against a supplied JSON Schema.
- `local_classify` — batch classification with an explicit label set and rubric.
- `local_review_diff` — first-pass diff review against an explicit checklist.

The adapter calls LiteLLM through `/v1/chat/completions`. It does not expose a
generic arbitrary-prompt tool, perform file writes, run shell commands, or make
final security and architecture decisions.

## Configure LiteLLM

Set these variables in the environment inherited by the MCP process:

```bash
export LITELLM_BASE_URL="http://<cluster-host>:<port>/v1"
export LITELLM_MODEL="local-30b"
# Optional when the trusted LiteLLM listener requires a key:
export LITELLM_API_KEY="..."
```

Useful limits:

```bash
export LOCAL_WORKER_MAX_INPUT_CHARS=50000
export LOCAL_WORKER_MAX_OUTPUT_TOKENS=4000
export LOCAL_WORKER_TIMEOUT_MS=120000
export LOCAL_WORKER_TEMPERATURE=0
```

`LOCAL_WORKER_MAX_OUTPUT_TOKENS` has to cover hidden reasoning as well as the
answer. Reasoning models such as `muse` spend the budget on reasoning first, so
a cap that only fits the answer returns an empty response with
`finish_reason=length` rather than a truncated one. The adapter reports that
case as `output_budget_exhausted`. A 3KB diff review needs roughly 2700
completion tokens against `muse`; 4000 leaves headroom. Raise it further for
larger reviews, and raise `LOCAL_WORKER_TIMEOUT_MS` with it — reasoning output
is slow, and that same review takes around 40 seconds.

The local model's output is still nondeterministic. The adapter requires JSON,
validates extraction results against the supplied schema, attaches source
evidence and route metadata, and returns a tool error when the model produces
invalid or schema-incompatible output.

## Codex and Claude Code: stdio mode

Stdio is the lowest-overhead option when Codex and Claude Code run on the same
workstation. Each harness launches the adapter, and the adapter calls LiteLLM
over the LAN.

Run it directly for a smoke test:

```bash
npm run mcp:local-inference
```

Codex `~/.codex/config.toml`:

```toml
[mcp_servers.local_inference]
command = "node"
args = ["/absolute/path/to/claude-model-router/mcp/local-inference-mcp.js"]
```

Claude Code:

```bash
claude mcp add --transport stdio local-inference -- \
  node /absolute/path/to/claude-model-router/mcp/local-inference-mcp.js
```

The environment variables must be present in the process environment used by
the harness. Verify with `codex mcp list` or `claude mcp list`.

## Streamable HTTP mode

Use HTTP when the adapter should be shared by multiple workstations or run
inside the cluster:

```bash
MCP_TRANSPORT=http \
MCP_HOST=127.0.0.1 \
MCP_PORT=8787 \
npm run mcp:local-inference
```

The MCP endpoint is `http://127.0.0.1:8787/mcp`; health is
`http://127.0.0.1:8787/healthz`.

For a non-loopback bind, set an optional bearer token and keep the service on
the trusted network:

```bash
export MCP_BEARER_TOKEN="replace-with-a-cluster-token"
export MCP_HOST="0.0.0.0"
MCP_TRANSPORT=http npm run mcp:local-inference
```

Codex:

```toml
[mcp_servers.local_inference]
url = "http://<mcp-host>:8787/mcp"
```

Claude Code:

```bash
claude mcp add --transport http local-inference \
  http://<mcp-host>:8787/mcp
```

Set `MCP_HTTP_JSON_RESPONSE=0` only when a client specifically needs the
Streamable HTTP SSE response form. JSON responses are preferable for these
short request/response worker calls.

## Operating model

Treat results as candidate evidence for the frontier model. Use the local
worker for repetitive semantic work and large batches; keep final architecture,
security, file writes, commits, deployments, and user-facing decisions in
Codex or Claude Code.

## Resource verification

The repository includes one bounded, non-generative preflight for each harness.
Codex uses `scripts/verify-local-inference-resource.js` and Claude Code uses
`scripts/verify-local-inference-claude-resource.js`.

Both checks:

- confirms that LiteLLM advertises the configured `muse` model;
- starts the configured stdio MCP worker and verifies the expected tool surface;
- rejects request, tool, and worker timeouts above the configured limits.

Codex additionally verifies the `local-muse-worker` provider route. Claude Code
uses a bounded `local-muse-worker` agent whose semantic work is performed by the
MCP-backed `muse` model; Claude's agent frontmatter does not expose Codex's
per-agent `model_provider` field. Claude runs its check at session start and
before local MCP calls or local-worker spawns. Failed checks deny only the local
route and instruct the harness to keep the task on a frontier worker. Results
are cached briefly so repeated bounded tasks do not repeatedly start the MCP
process.

Run the checks manually with:

```bash
node scripts/verify-local-inference-resource.js --no-cache --target=any
node scripts/verify-local-inference-claude-resource.js --no-cache
```

Claude Code project setup is checked into `.mcp.json`,
`.claude/agents/local-muse-worker.md`, and `.claude/settings.json`. On first use,
approve the project-scoped `local-inference` server when Claude Code prompts for
MCP approval, then confirm it with `claude mcp list`. The checked-in MCP entry
uses this workstation's absolute adapter path and trusted LAN address; change
both values when reproducing the setup on another machine.

For large artifacts, pass bounded excerpts or stable snapshot identifiers
rather than repeatedly copying whole repositories into tool arguments. For
long-running jobs, add an explicit queue/job wrapper later instead of holding a
single MCP call open indefinitely.
