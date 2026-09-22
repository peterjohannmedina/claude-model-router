# Local inference MCP tools

The adapter delegates bounded semantic work to a configured OpenAI-compatible
Chat Completions endpoint. Its tools are `local_extract`, `local_classify`, and
`local_review_diff`. It has no filesystem-write, shell, or generic agent tools.
The parent provides bounded content and validates the returned candidate evidence.

## Configure the endpoint

Install dependencies with `npm ci --ignore-scripts`. Set these variables in the
environment inherited by Claude Code or the MCP process:

```sh
export LITELLM_BASE_URL="http://your-model-host:4000/v1"
export LITELLM_MODEL="your-model-route"
# Set LITELLM_API_KEY through your environment/secret manager when required.
```

On Windows, use the equivalent PowerShell `$env:NAME = 'value'` syntax. Do not
commit credentials to `.mcp.json`, agent profiles, or task packets.

| Variable | Adapter default | Project configuration |
|---|---|---|
| `LITELLM_BASE_URL` | Required | Inherited from environment |
| `LITELLM_MODEL` | `local-30b` | Configure explicitly for the preflight |
| `LITELLM_API_KEY` | Unset | Inherited when provided |
| `LOCAL_WORKER_MAX_INPUT_CHARS` | 50000 | Aggregate bounded task content |
| `LOCAL_WORKER_MAX_OUTPUT_TOKENS` | 4000 | 4000; preflight minimum 2000 |
| `LOCAL_WORKER_TIMEOUT_MS` | 120000 | 60000 |
| `LOCAL_WORKER_TEMPERATURE` | 0 | 0 |
| `LOCAL_WORKER_MAX_UPSTREAM_RESPONSE_BYTES` | 2000000 | Response-body cap |

Reasoning models may consume the completion budget before emitting answer text.
The adapter reports empty budget-exhausted responses as errors. Increase a budget
only within your deployment's limits, or reduce the task size; catalog presence
alone does not prove a model can complete a real request.

Extraction output is checked against the supplied JSON Schema. Classification
must return one unique valid index and allowed label per item. Review output
must match the finding schema and requested finding limit. Validation rejects
malformed output but does not establish that model reasoning or evidence is true.

## Claude Code project integration

The checked-in provenance checks require Claude Code 2.1.274 or later. Earlier
versions do not supply the server-source field and the local MCP hook fails closed.

This checkout contains `.mcp.json`, `.claude/settings.json`, and a bounded native
Haiku dispatcher named `local-muse-worker`. The name is retained for compatibility;
the actual local model is selected by configuration. Its only tools are the three
MCP semantic tools.

Launch Claude Code from this checkout with the endpoint and model environment
configured. The project MCP entry uses `${CLAUDE_PROJECT_DIR:-.}` for the adapter
path: Claude expands the explicit variable when provided, or uses the launch
directory fallback. It uses Claude's millisecond `timeout` setting. Accept the project's MCP server
through Claude's normal approval UI and confirm it with `claude mcp list`.

The hooks run a bounded, non-generative preflight: validate configuration and the
exact agent/tool surface, check `/models`, start the known adapter, and list its
tools. Invalid configuration is rejected before network or subprocess probes.
`PreToolUse` can deny a local-worker spawn or MCP call; MCP calls must identify
the expected project-scoped server. `SessionStart` and `SubagentStart` provide
context. **SubagentStart cannot block a spawn.** A short cache reduces repeated
health checks; configuration changes invalidate it, while liveness may change
between a check and a request.

```sh
node scripts/verify-local-inference-claude-resource.js --no-cache
```

The routing installer does not copy this project MCP configuration into unrelated
projects. For another project, configure its own server and hooks deliberately.
The checked-in verifier expects the matching project adapter layout and profile;
it is not a universal validator for arbitrary MCP commands.

See Claude's official [MCP configuration](https://code.claude.com/docs/en/mcp),
[hooks](https://code.claude.com/docs/en/hooks), and
[subagent configuration](https://code.claude.com/docs/en/sub-agents).

## Other clients and HTTP transport

The stdio adapter can also be registered with Codex using an absolute path:

```toml
[mcp_servers.local_inference]
command = "node"
args = ["/absolute/path/to/claude-model-router/mcp/local-inference-mcp.js"]
```

The legacy Codex resource verifier checks its documented MCP-specific provider
layout. A directly configured Codex `muse-worker`/`litellm` provider is a different
route; the verifier reports that mismatch and does not rewrite Codex settings.

For Streamable HTTP:

```sh
MCP_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=8787 npm run mcp:local-inference
```

The endpoint is `/mcp`, with `/healthz` for health. A non-loopback bind requires
`MCP_BEARER_TOKEN`; supply it via the environment and configure client bearer
authentication. Loopback listeners validate Host/Origin to resist DNS rebinding.
Bearer authentication does not encrypt HTTP. Use trusted infrastructure or a TLS
proxy when crossing a network. `MCP_HTTP_JSON_RESPONSE=0` enables SSE responses
for clients that need them.
