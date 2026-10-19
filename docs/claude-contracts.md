# Claude Code integration contracts

Model/effort contracts checked **2026-10-17 UTC**; telemetry contracts refreshed **2026-10-19 UTC**. These are mutable documentation observations, not verification of an installed Claude version, account, subscription, or live inference. Research and tests made no model calls and changed no installed Claude configuration.

## Model selection and effort

Claude Code documents `haiku`, `sonnet`, `opus`, `fable`, and `best`; aliases vary by provider and update over time. `default` clears an override; `opusplan` uses Opus for planning and Sonnet for execution. The current Anthropic API alias table names Haiku/Sonnet/Opus 5.5. Full model IDs can pin versions.

The current 5.5 family supports `low`, `medium`, `high`, `xhigh`, and `max`; Opus/Sonnet 4.6 omit `xhigh`. Unsupported levels can be reduced. Effort scales differ across models. `ultracode` controls orchestration; `ultrathink` adds a prompt instruction without changing API effort.

Interactive `/model <name>` normally saves the user default; picker `s` selects this session only. `/status` shows the active model. Managed restrictions, aliases, and resume behavior can affect the actual selection. These behaviors have version-specific exceptions; avoid assuming every installed release matches this snapshot. [Official model configuration](https://code.claude.com/docs/en/model-config)

The CLI exposes `--model`, `--effort`, `--continue`, and `--resume`. `--effort` is session-scoped and available levels depend on the model. `--agents` supplies custom agents dynamically. Flag support should be checked against the documented version, not inferred solely from `--help`, which does not list every flag. [Official CLI reference](https://code.claude.com/docs/en/cli-reference)

**Router contract:** return a recommendation without changing Claude's active model, global settings, environment, or permissions. When showing a launch example, use `claude --model sonnet --effort medium` only after the operator confirms that pair. Describe picker session selection separately from a persistent `/model <name>` command. Keep special orchestration modes outside the model/effort enum.

The internal `default` effort token means **leave Claude's effort control unchanged**. It is not an API effort value and must never become `--effort default`. Use it for a confirmed route whose model offers no effort control, or when the operator intentionally leaves effort unmanaged. Never copy Codex's `ultra` level into a Claude route.

## Subscription eligibility and catalog freshness

The Models API explicitly describes models available **in the API**; it is not a documented Claude Code subscription entitlement endpoint. [Official Models API](https://platform.claude.com/docs/en/api/models/list)

Claude Code supports claude.ai login, Console/API access, and third-party providers. Authentication precedence can select a different credential source than an operator expects. [Official authentication guide](https://code.claude.com/docs/en/authentication)

**Router contract:** do not port Codex's app-server `account/read` or `model/list` RPCs. Do not parse credentials, private caches, browser storage, or hidden account endpoints. A documented alias is a suggestion until the operator checks the active Claude `/model` picker and confirms both the model and available effort choices. A gray or restricted picker row is not confirmation. An API catalog result must remain API-scoped even if the name matches a native route.

`scripts/routing_catalog.py` therefore uses a local confirmation import:

```json
{
  "confirmed": true,
  "source": "claude_code_model_picker",
  "producer_version": "2.1.293",
  "provider": "claude_ai",
  "models": [
    {"id": "sonnet", "efforts": ["low", "medium", "high"], "default_effort": "medium"}
  ]
}
```

This example is a **fake test observation**, not a claim about the user's account. Import timestamps are assigned locally. The current policy expires confirmations after 15 minutes; expiration is a router policy, not an Anthropic guarantee. Refresh without a new explicit confirmation fails with `native_picker_confirmation_required`. It never silently re-dates the prior observation.

Public status retains `queried` for dashboard compatibility but reports `entitlement: user_confirmed`, `scope_verified: false`, and `remote_freshness: unverified`. The source is `claude_model_picker_confirmation`. Missing confirmation exposes only an ineligible `documented_alias_baseline`. Before routing, intersect fresh confirmation, saved selected IDs, packet candidates, effort choices, and required modalities. Repeat the check after login, provider, restrictions, or version changes. Operator confirmation remains weaker than a provider entitlement attestation.

An explicitly ineligible `policy_supported: false` row invalidates the import or cached confirmation; normalization never upgrades it to eligible.

## Hooks and telemetry

Official hooks receive JSON over stdin or HTTP. Common metadata includes `session_id`, optional `prompt_id`, and sometimes `effort.level`. Only `SessionStart` can include `model`, and it can be absent. `PostModelSwitch` uses `from_model`/`to_model`, requires v2.1.251, and observes session changes including automatic fallback; a temporary per-turn fallback chain does not fire it. `PostCompact` supplies `compact_summary`; `Stop`/`SubagentStop` can supply assistant text. The documented common hook schema does not require a producer version. [Official hooks reference](https://code.claude.com/docs/en/hooks)

Status-line JSON separately documents `version`, `model.id`, `effort.level`, and optional rate limits. It is a distinct input contract; do not substitute it into hooks. [Official status-line schema](https://code.claude.com/docs/en/statusline)

The optional separate status-line collector now supports ongoing snapshots. Its context counters describe the current context/latest response, not cumulative session usage; cost is a client estimate. Snapshot model labels describe observed session configuration, not verified responding models. See [ongoing telemetry](continuous-telemetry.md) for cadence, exact privacy boundaries, configuration declarations and preserving an existing display. No OTLP receiver is added by this collector.

OTel uses resource `service.version`; Desktop Code sessions can report the Desktop app version. The API request event uses `event.name: api_request`, model/effort metadata, and token fields. Its input tokens exclude cache reads/writes. Export is opt-in, and optional logging switches can expose prompts or tool content. [Official monitoring guide](https://code.claude.com/docs/en/monitoring-usage)

**Router contract:** `routing_otel.py` retains the shared adapter filename/API but currently accepts hook observations only. OTLP payloads are ignored as unsupported transport. No OTel producer release is declared live-verified. Enabling an OTel exporter requires a separate reviewed adapter and explicit setup; never copy Codex event names, version fields, or exporter settings into Claude configuration.

The hook allowlist is `SessionStart`, `Stop`, `PostCompact`, `SessionEnd`, `SubagentStart`, `SubagentStop`, and `PostModelSwitch`. Only validated IDs, model/effort labels, fixed lifecycle categories, and optional numeric version annotation reach the ledger. The adapter never opens `transcript_path`, expands `cwd`, stores summaries or assistant text, hashes prompts, or retains unknown fields. It discards summary content even when a payload is otherwise accepted. This minimizes content exposure; a summary is untrusted evidence and cannot authorize model switches or provider spending.

Hook observations have `source: claude_hook`, `session_source: claude_thread`, and `contract_validation: documented_hook_schema`. They do not claim a completed execution, token usage, billed cost, or a verified per-request responding model. `Stop` is a lifecycle observation, not proof of task success. Version supplied through the adapter envelope or collector flag is an **operator annotation**, not an Anthropic hook field or attestation. Missing version is preserved as unknown. A supplied old version cannot enable `PostModelSwitch`.

UUID session/prompt IDs are accepted by this bounded adapter. Unsupported identity formats fail safely. Model values additionally pass the public model-ID grammar, excluding path separators; a path-shaped model is omitted from SessionStart, and an invalid switch endpoint causes that switch observation to be ignored. Prompt UUIDs provide best-effort deduplication. Without a prompt/event UUID, repeated identical lifecycle observations may collapse. An optional stable adapter `event_id` supports replay and is namespaced under the native session before deriving the ledger key, so two sessions can safely reuse it. There is no exactly-once or full-turn correlation claim.

The stdin collector handles one bounded hook input and emits no stdout by default, because hook stdout can become model context. Optional safe diagnostics go to stderr. It exits without a control decision and never blocks Claude. It installs no hook itself.

### Explicit hook setup example

After reviewing the active Claude version's hook contract, merge an entry like
this into the desired settings scope. Replace both absolute paths with the
existing Python executable and installed collector. This example is not applied
by the installer. Preserve all existing hooks and settings. Use JSON-escaped
backslashes for Windows paths or forward slashes, and test the command in the
shell used by Claude before enabling it. `--state-dir` can select the same
private state directory used by the dashboard when it differs from the default.

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "\"/absolute/path/to/python\" -B \"/absolute/path/to/.claude/skills/claude-model-routing/scripts/collect_claude_hook.py\"", "timeout": 5}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "\"/absolute/path/to/python\" -B \"/absolute/path/to/.claude/skills/claude-model-routing/scripts/collect_claude_hook.py\"", "timeout": 5}]}]
  }
}
```

Add other allowlisted events only when the installed producer documents them.
Remove only these added collector entries to disable collection; turning history
off in the dashboard also disables ledger writes. Collector stdin is never
copied into diagnostics. A silent exit means the hook observer did not block
Claude; confirm retained metadata separately in the dashboard, and use
`--diagnostics` only for the safe accepted/ignored/rejected counters.

## Supported programmatic control boundary

The official Agent SDK runs the Claude Code agent loop in a process the application operates, using Python or TypeScript. The Client SDK accesses the API directly. The CLI supports subprocess use for other languages. These are different products and execution boundaries. Anthropic's SDK overview says third parties require prior approval to offer claude.ai login or rate limits in their products. [Official Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)

The model guide documents Agent SDK `setModel()` and validation of requested IDs, with version-dependent provider checks. [Official model control](https://code.claude.com/docs/en/model-config#setting-your-model)

**Router contract:** this package recommends routes. It does not claim to control an unrelated terminal, Desktop conversation, or existing subscription session. A future execution adapter should own its SDK/CLI session, use documented controls, enforce spending/permission gates, and observe actual results separately. It must not treat a recommendation or catalog import as execution authorization. Re-check the SDK's then-current authentication and commercial conditions before shipping that adapter.

## Offline verification fixtures

Tests use synthetic picker and lifecycle JSON only. Cover missing/expired/future confirmation, forged API scope, malformed efforts, duplicate IDs, model selection intersection, missing hook model/version, post-switch minimum version, unknown events, summary/prompt/path exclusion, UUID validation, replay, byte limits, and duplicate JSON keys. Assert private sentinel content is absent from persisted SQLite bytes and diagnostics. Test lifecycle totals separately from execution/usage totals. No inference, credentials, real subscription catalog, or installed settings are needed.
