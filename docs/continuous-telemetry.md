# Optional ongoing Claude Code metadata

Contract check: **2026-10-19 UTC**. These are supported public input contracts,
not evidence that a particular running session or backend emits every field.
This package makes no model calls to discover telemetry support.

| Input | Coverage | Meaning and limit |
| --- | --- | --- |
| SessionStart hook | Startup/resume and other configured lifecycle triggers | A session/model observation; normally quiet during ongoing work |
| PostModelSwitch hook | Supported Claude Code versions, session model changes | Does not attest temporary per-request fallback or underlying weights |
| Status-line stdin | Startup/resume, assistant-message and interface updates; optional timer | Current session configuration, current-context counters and client session estimates |
| Native OTel | Public per-request event/metric contract | No OTLP decoder in this package; exporter activation remains out of scope |
| Claude Code with an Ollama-compatible backend | Same status-line/hook path if that Claude Code process emits it | Backend/provider and actual model execution are unverified; missing fields remain unknown |
| Other Ollama clients, Claude web/Desktop | No verified collector here | Never infer coverage from a model label or dashboard connection |

The [status-line contract](https://code.claude.com/docs/en/statusline) runs a
configured local command with JSON on stdin. It does not consume model tokens.
Event-driven updates can go quiet while idle. Optional `refreshInterval` adds a
timer (documented minimum one second); debounce and cancellation mean updates are
best effort. The minimum version for that timer is not stated. Do not add it to
an older installation without checking support. Installed package version and
the version emitted by an active session are separate observations.

`context_window.total_input_tokens` and `total_output_tokens` describe the
**current context, from the latest response**, not cumulative session usage.
Input includes cache reads/writes. They can shrink after compaction and must not
be summed across snapshots. Optional current usage can be absent/null. The
collector stores only its explicitly supported context counters; it does not
reconstruct usage by reading transcripts.

`cost.total_cost_usd` is a client estimate, possibly based on list price or a
custom pricing table. It is not a measured bill, especially through gateways or
Ollama. `total_duration_ms` accumulates running session time across resumes;
`total_api_duration_ms` measures waiting for API responses. Neither proves task
completion. These values are snapshots, not increments. Cost before Claude Code
2.1.211 could carry across `/clear`; old versions retain that accounting caveat.

The snapshot's model is **observed session configuration**, not a verified
responding model. An Ollama model alias does not attest underlying weights.
Ollama's [Anthropic compatibility](https://docs.ollama.com/api/anthropic-compatibility)
also documents approximate tokenizer counts and model-defined thinking controls.
The router never upgrades such metadata into subscription entitlement, a paid
execution, or model-dispatch authority.

## Collector and privacy

`scripts/collect_claude_statusline.py` is separate from the hook decoder. It reads
one bounded JSON object, validates session/version/model/numeric fields, and
writes only allowlisted metadata to the existing local ledger. No transcript,
prompt, assistant message, task description, tool arguments/results, workspace
path, repository identity or credential is retained. It never opens a path from
stdin and makes no network requests. Unknown fields are dropped. Invalid known
fields fail safely; missing optional fields remain unknown.

The full bounded input is parsed in memory before the allowlist is applied. It
can include private paths and session titles, even though these are never
retained or opened. Review that new persistent input access before activation.

The ledger keeps a single latest snapshot per native session. Identical valid
receipts update last-seen time without creating more observations or adding
usage/cost. Hook lifecycle rows and router/exec usage remain separate. Retention
and recording-disabled settings apply to this collector too.

Collection is best effort: the ledger has a bounded 250 ms lock wait. A busy
writer or failed/cancelled callback can drop a receipt without advancing
last-seen time. Successful concurrent writes serialize their counters; there is
no queue, exact receipt-count guarantee, or automatic inference retry.

By default the collector prints nothing. **It is not a transparent replacement
for an existing status-line display.** Optional `--forward-stdin` passes the
original bounded JSON object to stdout for an existing trusted display command;
that forwarding includes paths/other metadata already in the original stdin.
It never writes that raw object to history. Review the destination and shell
quoting before using this option. Malformed JSON and oversized/non-object input
are not forwarded. Collector schema drift, a storage failure or disabled
recording do not suppress forwarding of a bounded JSON object to the existing
display. The collector does not launch a shell or another command itself.

## Explicit activation and rollback

The installer does not enable this collector, change `statusLine`, or add hooks.
Back up the exact settings and existing status-line script before a change.
Preserve every existing status-line option, hook, environment and unrelated
setting. If a status line already exists, integrate the collector into that
user-owned script using the same stdin object, or carefully compose the explicit
forwarding option with its unchanged display command in the same supported
shell. Do not replace a working display with a silent collector.

Use event-driven collection first: leave `refreshInterval` absent. Review the
existing command as code before composing it; a status-line display may also
write caches or routing-policy state. A timer reruns the **entire command**,
including those existing side effects, rather than just the metadata collector.
Adding a timer therefore needs a separate scope decision.

On Windows, the documented runner uses Git Bash when available and PowerShell
otherwise. Do not copy an unquoted `C:\\Users\\...` command into a Bash pipeline:
backslashes can be interpreted as escapes. For a verified Bash runner, use
quoted paths with forward slashes. A command with one existing executable and
script argument can be composed like this (the actual shell still needs to be
verified):

```sh
"C:/absolute/python.exe" -B "C:/absolute/collect_claude_statusline.py" --state-dir "C:/absolute/routing" --forward-stdin | "C:/absolute/bash.exe" "C:/absolute/existing-statusline.sh"
```

This preserves the existing script, arguments and inherited environment; it
does not claim literal byte identity with a wrongly escaped command string.
Do not use this Bash syntax as a PowerShell command. Validate quoting, exact
stdin forwarding, display output and downstream exit status with inert
substitutes and synthetic input first. Do not execute an unreviewed existing
display just to check composition. A missing collector/module or interpreter
startup failure can prevent forwarding, so verify the complete installed
package before changing settings. No-timer installation/activation remains a
separate reviewed local action from code or PR preparation.

For a configuration **without an existing status line**, a command entry can
point to the installed collector (replace absolute paths and use the quoting
required by Claude's actual command shell):

```json
{
  "statusLine": {
    "type": "command",
    "command": "\"/absolute/python3\" -B \"/absolute/.claude/skills/claude-model-routing/scripts/collect_claude_statusline.py\" --state-dir \"/absolute/.claude/routing\""
  }
}
```

That example intentionally renders no display and adds no timer. Timer refresh is a separate
choice: after verifying support, `"refreshInterval": 10` gives a ten-second
metadata receipt cadence while the status line runs. Timer receipts show that
the script ran, not that a model request or useful work occurred. Claude may
reload a changed command immediately; coordinate activation around active work.

After reviewing settings, an operator may declare expected collectors in
`routing/telemetry-registration.json`:

```json
{
  "schema_version": 1,
  "declared_at": "2026-10-19T00:00:00+00:00",
  "collectors": ["session_start", "statusline"]
}
```

Use the actual activation timestamp. This is an **operator declaration**, not
proof of effective settings or a live feed. A missing declaration remains
unknown even if historical rows exist. The dashboard reports retained last-seen
times and a five-minute freshness window independently of its optional HTTP
hook receiver. Old records and a green dashboard connection do not establish
ongoing receipt. Silence can mean idle event-driven updates, shutdown, a storage
problem or an unsupported configuration; the dashboard cannot distinguish them.

To undo activation, restore only the reviewed `statusLine` change (or the exact
backup if settings have not changed), restore its script if edited, and remove
only the corresponding registration name. Preserve concurrent settings changes.
Package uninstall does not undo user-owned settings, scripts or registrations.
Disable/remove the integration first so Claude does not call a removed file.
Existing history can be retained or cleared separately.

## Verification boundary

Automated tests use frozen synthetic inputs and temporary state: privacy
sentinels, invalid schemas, multiple sessions, repeated receipts, context shrink,
disabled recording and forwarding/storage failures. No paid inference runs.
Cross-platform CI checks the same code on Windows, macOS and Linux.

Live acceptance still requires an explicitly approved integration with the
user's existing display: observe genuine receipts from a consenting session,
check emitted version and last-seen updates, then verify model/context/timing
fields only when present. Do not inject fixture rows into production to claim
pickup, restart active work, or use transcript/content logging to fill gaps.
