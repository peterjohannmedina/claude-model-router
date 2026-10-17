# Claude adaptation of the Codex router

The reference implementation is
[`codex-model-routing` main at e8d686d](https://github.com/peterjohannmedina/codex-model-routing/tree/e8d686d2c844399e59bacb71824cf6d379c4a0f8).
Its PR7–10 work supplied the portable provider, dashboard, local-only ledger,
decision provenance, synthetic benchmark and packaging patterns. Claude's
discovery, effort and telemetry contracts are evaluated separately; this is not
a claim that Codex and Claude expose equivalent APIs.

The lifecycle port also applies the reviewed changes from source
[draft PR11 at 5dc8db3](https://github.com/peterjohannmedina/codex-model-routing/commit/5dc8db35190bb16563afabaa52e096f234d187aa):
bounded stale-state recovery and Windows launch failure diagnostics. The source
commit is a reference, not an assumption that PR11 has merged or been installed.

| Layer | Claude adaptation | Evidence boundary |
| --- | --- | --- |
| Native routing | Validated model/effort recommendations with explicit overrides | No automatic dispatch into an existing interactive session |
| Native catalog | Explicit confirmation from Claude Code's `/model` picker, source and timestamp retained | API model discovery and checked-in aliases do not establish subscription entitlement |
| Optional evaluator | OpenAI Decisions chooses among admitted complete candidates; disabled by default | Explicit API permission and configured credential required; rules fallback records its cause |
| Optional local worker | Generic configured Chat Completions endpoint and exact advertised model | Readiness is not inference success; execution needs separate flags and bounded input |
| Dashboard | Authenticated loopback UI, private launch handoff, optimistic config updates, session filters | UI preferences affect router policy, not Claude's current pinned selection |
| Telemetry | Redacted metadata from explicitly configured supported Claude hook events | Unknown harness/version/field coverage remains unknown; prompts and transcripts are not collected |
| Benchmark | Frozen synthetic fixtures and caller-supplied outcomes | Confidence is evaluator certainty, never a quality measurement |
| Lifecycle | Versioned package, detached dashboard, explicit startup choice, guarded rollback | Installed config and live provider validation remain separate opt-ins |

The existing Node.js MCP adapter, resident-worker helpers and local-target policy
remain compatibility paths. Their historical names do not configure a default
local endpoint or authorize use. New provider settings are separate from those
legacy counters and do not migrate or reset their history.

Security constraints: no raw prompt, transcript, tool arguments, credential
values or provider error bodies enter the ledger. The browser receives a short
bootstrap capability via an expiring local handoff; requests then require the
authenticated session and CSRF token. The service binds loopback and validates
Host/Origin. It does not install a network listener on external interfaces or
modify machine security policy. Keep state and backups outside Git.

See [verified Claude contracts](claude-contracts.md), the README's verification
limits, and the test suites for what is actually implemented.
