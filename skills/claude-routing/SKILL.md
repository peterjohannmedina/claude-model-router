---
name: claude-routing
description: Read or set the persisted Claude local-worker target and waiting policy. Use when the user requests a routing policy change, including /claude-routing with an optional percentage.
argument-hint: [TARGET_PERCENT]
---

# Configure local-worker routing

For `/claude-routing`, no argument sets a 50% local-worker target. Accept a bare
integer from 0 through 100 or `TARGET_PERCENT=75`. If the user only asks to view
the current policy, use `get` without changing it.

Resolve `../claude-model-routing/scripts/manage-routing-policy.js` relative to
this skill directory. Run it with Node, for example:

```sh
node ../claude-model-routing/scripts/manage-routing-policy.js set --target 75
```

Keep the existing waiting policy unless the user explicitly requests a change;
new policies default to `wait_for_results=true` and 1,800 seconds. Use
`--wait true|false` and `--timeout SECONDS` only when requested. Read back the
returned JSON and report target, waiting policy, eligible-task count, and local
share. Never reset history as a side effect of changing a target.

The target covers eligible bounded worker opportunities only. It cannot override
secret handling, task suitability, capacity, parent validation, or permissions.
A setting of zero disables policy-driven local dispatch. Waiting disabled means
the parent may continue independent work; results must still be retrieved and
validated before use. Changes persist under `CLAUDE_CONFIG_DIR` or `~/.claude`.
