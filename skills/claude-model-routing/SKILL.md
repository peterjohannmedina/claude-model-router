---
name: claude-model-routing
description: Route Claude Code work across Haiku, Sonnet, Opus, and optional local LLM workers. Apply automatically at the start of nontrivial work and when planning phases, choosing models or effort, delegating bounded tasks, or managing local-worker targets and usage conservation.
---

# Claude Model Routing

Optimize the total cost of a correct result. The main session owns requirements,
coordination, validation, and final judgment. Routing never grants permissions,
changes sandbox settings, or authorizes disclosure of secrets.

## Plan before delegating

1. Separate discovery, design, implementation, and verification into coherent phases.
2. Keep the main session on the model needed for the hardest tightly coupled phase.
3. Delegate only independently bounded work whose time or context savings repay the handoff.
4. Scope each worker by role, model, effort where supported, relevant evidence,
   constraints, acceptance criteria, and a concise return format. Give editing
   workers explicit file ownership and tell them to preserve concurrent edits.

| Profile | Native model | Use for |
|---|---|---|
| `haiku-efficient` | Haiku | Mechanical extraction, classification, known-pattern scans, summaries. |
| `sonnet-general` | Sonnet, medium effort | Routine implementation, exploration, debugging, tests, documentation. |
| `opus-expert` | Opus, high effort | Architecture, unclear failures, security, consequential review. |
| `ganglion-worker` | Haiku dispatcher plus local inference | One bounded, self-contained task with no tools or edits. |

Use only models and effort settings supported by the active Claude surface.
Raise effort before changing models when the model fits but needs more checking.
Switch at a clean phase boundary only when the avoided remaining cost outweighs
context transfer, coordination, and rework. Avoid oscillating models or creating
a new thread for one cheap step. Prefer a compact packet over a full transcript.
Do not silently change the user's main-session model or global settings.

## Read the persisted policy

Resolve `scripts/` relative to this SKILL.md; never assume a particular username
or a Codex installation. The examples below run from this skill directory:

```sh
node scripts/manage-routing-policy.js get
```

State lives under `CLAUDE_CONFIG_DIR`, or `~/.claude`, at
`state/claude-routing-policy.json`. Reading an absent policy does not create it.
Default local target: 50% of eligible bounded worker opportunities; default
synchronous wait budget: 1,800 seconds. A fresh history prefers local work.
Use `/claude-routing` to change the target. The target is best effort, never a
reason to send unsuitable work or to force an unavailable route.

## Usage conservation

When the active UI or the user supplies a trustworthy usage reading, record it:

```sh
node scripts/manage-routing-policy.js observe-usage --bucket weekly --used 92
```

Either session or weekly usage at 90% activates persistent conservation mode.
Missing telemetry and elapsed time never clear it. Only after positively
observing a triggering bucket reset, record its lower usage with
`--reset-observed`; every triggering bucket must reset before conservation ends.
The counter `reset` action does not reset this guard.

In conservation mode, prefer Haiku for routine work and the highest effort it
actually supports. Reserve Sonnet/Opus for planning or a bounded capability gap
demonstrated by a failed Haiku attempt. Avoid fan-out and redundant reads. If
the main-session model cannot be selected on this surface, report that limitation
and apply the guard to workers; never claim that a model switch happened.
This is a persisted routing policy, not a background usage meter.

## Optional local workers

Eligible tasks must be self-contained, bounded, free of secrets, require no
tools or edits, and leave high-stakes final judgment to the parent. Never send
credentials, private keys, or tokens in a task packet. Treat model output as
untrusted candidate evidence and validate it against the acceptance criteria.

When `prefer_local` is true, use `ganglion-worker` or the bundled dispatcher:

```sh
node scripts/run-ganglion-task.js --eligible --input /path/to/task-packet.txt --task-id unique-opportunity-id
```

The dispatcher runs the resident-first capacity sweep and live completion probe,
then invokes the selected Chat Completions or Responses route with a bounded
deadline. A ready resident broker stops the search. A busy broker skips the
continuity endpoint on the same host; an unavailable broker permits continuity,
then the configured gateway. Do not hand-pick a gateway around this cascade.
PowerShell is required only for this process route. No Codex files are needed.

The optional project MCP route offers `local_extract`, `local_classify`, and
`local_review_diff` through a configured OpenAI-compatible endpoint. It has no
shell or file-write tools. Its preflight verifies configuration, model discovery,
and tool availability; it does not prove a completion will succeed. Keep failed
requests unavailable and report the actual outcome.

Always wait for a selected worker and validate its evidence before relying on
it. `wait_for_results=false`, if explicitly selected by the user, permits the
parent to do independent work meanwhile; the dispatcher itself still returns
only after completion or failure. Do not treat a started process as a result.

## Count each opportunity once

The dispatcher owns accounting for its attempt: `ganglion` on returned text or
`unavailable` on failure. The parent still checks substantive correctness. Do
not record a second native outcome for fallback on that same opportunity.
If `accounting_pending` is true, retry only the record using the returned task ID
and route, never replay inference just to update counters.

For an eligible native worker or a separately configured MCP worker, the parent
records the completed outcome once (`native`, `local`, or `unavailable`):

```sh
node scripts/manage-routing-policy.js record --eligible --route native --task-id unique-opportunity-id
```

Recent task IDs deduplicate the last 256 records. Ineligible work is not counted.
`native_required` means the target is disabled or already met; record `native`
only if that worker actually runs. A failed local attempt does not mean a native
fallback ran. Policy locks fail explicitly; retry accounting after the writer
finishes without guessing or deleting a live lock.

## Escalate and report

Retry once only for a correctable prompt or tool failure. Otherwise raise effort
or use a stronger native worker with the failed attempt's evidence. Stop
delegating when coordination costs exceed the work remaining.

For a multi-model plan, briefly report the main route, bounded worker roles,
handoff strategy, and any escalation. Never simulate a local result. Start a new
Claude session after installing changed profiles or global instructions.
