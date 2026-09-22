# Local workers and routing state

The routing installer is dependency-free Node code. The Ganglion process route
also requires `powershell.exe` on Windows or `pwsh` on macOS/Linux. All of its
scripts are bundled under `skills/claude-model-routing/scripts`; no Codex install
is required. Keep the route unavailable if PowerShell or a configured endpoint
is missing.

## Resident-first selection

The sweep uses this ordered cascade:

1. Resident broker: health, model catalog, runtime state, in-flight requests,
   leased slots, queue depth, and a bounded completion probe.
2. Continuity endpoint, only if the broker is unavailable. A busy broker skips
   continuity to avoid scheduling more work on the same resident host.
3. External gateway, after resident capacity is busy or unavailable.

A ready route stops the cascade. The process does not query a gateway merely to
compare it with an already-ready resident route. All endpoint/token settings are
deployment configuration; packet contents must never include secrets.

| Setting | Deployed default |
|---|---|
| `GANGLION_BASE_URL` | `http://127.0.0.1:8471/v1` |
| `GANGLION_MODEL` | `ganglion` |
| `GANGLION_API_KEY_ENV` | `HELIOS_API_TOKEN` |
| `GANGLION_CONTINUITY_BASE_URL` | `http://127.0.0.1:8472/v1` |
| `GANGLION_CONTINUITY_MODEL` | `ganglion` |
| `GANGLION_CONTINUITY_API_KEY_ENV` | Same as resident token variable |
| `GANGLION_GATEWAY_BASE_URL` | Unset; external gateway is opt-in |
| `GANGLION_GATEWAY_MODEL` | `ganglion-auto` |
| `GANGLION_GATEWAY_API_KEY_ENV` | `GANGLION_API_KEY` |

The `*_API_KEY_ENV` settings name an environment variable containing a scoped
token. They are not token values. Supply credentials through the host environment
or its secret manager. Set `GANGLION_GATEWAY_BASE_URL` explicitly to enable an
external gateway. A gateway token alone never enables a fixed LAN destination.
Use URLs for infrastructure you trust; use HTTPS where network transport needs
encryption. The original deployment used a LAN gateway, which remains supported
when configured explicitly.

To inspect capacity without generation, run from this repository:

```powershell
powershell.exe -NoProfile -File .\skills\claude-model-routing\scripts\sweep-ganglion-resources.ps1 -SkipCompletion
```

Omitting `-SkipCompletion` performs a bounded completion probe. Use `pwsh` in
place of `powershell.exe` on other platforms. The standalone
`test-ganglion-access.ps1` supports explicit `-BaseUrl`, `-Model`, `-ApiKeyEnv`,
and `-WireApi ChatCompletions|Responses` for diagnosing a particular route.

## Dispatch and accounting

Prepare one task file containing objective, supplied evidence, constraints,
acceptance criteria, and a concise return format. Screen it for local-worker
eligibility before dispatch. From the installed skill directory:

```sh
node scripts/run-ganglion-task.js --eligible --input /path/to/packet.txt --task-id opportunity-123
```

The dispatcher reads policy, runs the sweep with its completion probe, then runs
the selected worker. Packet input is capped at 50,000 characters; output tokens
default to 512 and can be set from 1 to 4096 using `--max-tokens`. Process output
is capped at 256 KiB, and the total policy wait budget includes discovery.
Large reasoning models may need a higher token budget. Failures return
`unavailable`, never fabricated output.

`succeeded` means the worker returned text. The parent must still validate its
content. `native_required` means the configured local target is disabled or met;
it does not claim a native worker ran. `accounting_pending` preserves completed
evidence if a state lock prevented recording: retry the record with the returned
task ID and `accounting_route`, not the inference. Recent IDs deduplicate 256
records; they are not a distributed inference-job queue.

One opportunity has one owner and outcome. Dispatcher attempts record themselves;
the parent records separately invoked native or MCP workers. A local failure and
its native fallback are one opportunity, recorded as `unavailable`.

```sh
node scripts/manage-routing-policy.js record --eligible --route local --task-id mcp-opportunity-123
```

`local` counts a successful separately configured local route. `ganglion` counts
the resident/gateway cascade. Both contribute to local share. `native` and
`unavailable` also count in eligible opportunities. Ineligible tasks are excluded.

## Persistent controls

The state manager defaults to
`$CLAUDE_CONFIG_DIR/state/claude-routing-policy.json`, or
`~/.claude/state/claude-routing-policy.json`. All commands accept `--state PATH`
for a separate policy. Existing deployed version-1 counters and wait settings are
preserved when newer fields are added.

```sh
node scripts/manage-routing-policy.js get
node scripts/manage-routing-policy.js set --target 75 --wait true --timeout 1800
node scripts/manage-routing-policy.js observe-usage --bucket weekly --used 92
node scripts/manage-routing-policy.js observe-usage --bucket weekly --used 3 --reset-observed
```

`get` never writes. `set` changes only supplied controls. Updates use an exclusive
lock and an atomic replacement. A lock conflict requires retry after the other
writer finishes. Corrupt state fails explicitly rather than resetting counters.
If a process crashes holding a lock, confirm its recorded PID is no longer active
before recovering that lock manually.

Usage readings must come from an actual visible reading or the user. At 90%
usage, conservation persists until every triggering bucket has an observed reset.
Lower usage without `--reset-observed` does not clear it. `reset` clears outcome
history only, preserving the target, waiting controls, and conservation guard.

Setting `--wait false` permits the parent to work on independent tasks while the
worker runs. The dispatcher remains a foreground process and returns only after
completion or failure; it does not implement an unattended background job queue.
