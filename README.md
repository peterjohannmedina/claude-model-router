# Claude Model Router

Model routing for Claude Code, with a built-in option to delegate bounded
subagent work to local LLMs. The main Claude session stays responsible for
requirements, validation, and final judgment.

The package combines native Haiku/Sonnet/Opus worker profiles, persistent routing
controls, and two optional local inference paths: a resident-first process worker
and an MCP adapter with extraction, classification, and first-pass diff review.
Local workers receive only supplied task content and have no tools or file access.

## Install routing

Requires Node.js 20.11 or newer and Claude Code with skills and custom subagents.
From this checkout:

```sh
node scripts/install.js --dry-run
node scripts/install.js
```

The installer adds two skills, four agent profiles, and a marked instruction
block under `~/.claude` (or `CLAUDE_CONFIG_DIR`). It preserves unrelated
instructions, settings, and routing history. It does not register MCP servers or
change your selected model. Start a new Claude Code session after installation.
Upgrades replace the two package-owned skill directories, removing obsolete
package files; keep personal extensions in separate skill directories.

Use `--config-root /path/to/.claude` for another profile,
`--user-root /path/to/user` for a separate user home, or
`--skip-global-instruction` to omit the automatic-routing instruction block.

## Routing controls

Routing applies to nontrivial work and can also be invoked as
`/claude-model-routing`. Use `/claude-routing` for the default 50% local-worker
target, or `/claude-routing 75` to set a different target. Zero disables
policy-driven local dispatch.

| Capability | Behavior |
|---|---|
| Native workers | Haiku for mechanical work, Sonnet for everyday engineering, Opus for complex review and design. |
| Context-aware routing | Keep coherent work together; delegate only when the handoff is worthwhile. |
| Local-worker target | Persistent best-effort target for eligible bounded opportunities, with deduplicated outcome accounting. |
| Usage conservation | Persist a guard after an observed session or weekly usage reading reaches 90%; clear only after observed resets. |
| Local delegation | Validate capacity, wait for a bounded result, and return candidate evidence to the parent. |

These controls guide Claude; they do not automatically switch the main model or
read account usage in the background. Secrets, tool-dependent work, edits, and
high-stakes final judgment are excluded from local-worker packets.

## Optional local LLM delegation

Choose the integration that fits your deployment:

- **Resident process worker:** checks local capacity first, then a configured
  gateway. Supports Chat Completions and Responses, with a bounded deadline and
  persistent outcome accounting. Requires Windows PowerShell 5.1 or PowerShell 7.
  See [local worker setup](docs/local-workers.md).
- **MCP semantic tools:** connects to an OpenAI-compatible Chat Completions
  endpoint. Exposes only `local_extract`, `local_classify`, and
  `local_review_diff`, with validated inputs and model-returned JSON. Also usable
  from Codex. See [MCP setup](docs/local-inference-mcp.md).

Native routing works without either local integration. The checked-in project
MCP route remains unavailable until its endpoint and model are configured.

## Development and reconciliation

```sh
npm ci --ignore-scripts
npm test
npm audit
```

Tests use synthetic local HTTP servers; they do not call a real LLM. PowerShell
integration tests run when the runtime is installed and otherwise report skips.

See the [reconciliation and code review](docs/reconciliation.md) for the GitHub,
deployed-local, and Codex-router differences, fixes, and remaining boundaries.
