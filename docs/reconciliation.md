# Reconciliation and review — 2026-09-22

## Sources compared

| Source | Starting point | Finding |
|---|---|---|
| GitHub `peterjohannmedina/claude-model-router` | `bae985d566c37bab6eaf3d9e6cfcd28a21fa7087` | Initial MCP adapter, project hooks, one local dispatcher profile; no installable native-routing package. |
| Gitea `synchronic1` | No standalone `claude-model-router` or `claude-model-routing` repository found | Create a standalone private mirror without replacing the GitHub origin. |
| Deployed local Claude configuration | Routing skill, `/claude-routing`, Ganglion dispatcher, process scripts and persisted policy | Newer routing behavior existed outside GitHub; two probe scripts depended on the local Codex installation. |
| Codex router | `a41de9d519b33bec85453d454d6d07e16ba7aacc` | Source for native tiering, bounded delegation, resident-first sweep, local target and usage conservation. |

The GitHub repository and installed Claude routing skill were complementary,
not competing revisions of the same file tree. Reconciliation retains the MCP
adapter and packages the deployed routing capability alongside it. Runtime
credentials, user settings, session transcripts, and counter state are not part
of the distribution.

## Integrated features

| Codex capability | Claude implementation |
|---|---|
| Role-specific native agents | Haiku, Sonnet and Opus profiles with bounded handoffs and file ownership guidance. |
| Stay-versus-switch economics | Preserve coherent context, raise supported effort before changing model, avoid oscillation. |
| Resident-first local cascade | Self-contained sweep/probe scripts; no dependency on a user's `.codex` directory. |
| Bounded process inference | Preserve Claude's dual Chat Completions/Responses support; add UTF-8 handling, packet/output bounds and deadline wrapper. |
| Persistent local target | Portable state manager, deployed-state migration, explicit cold-start selection, atomic updates and deduplicated records. |
| Usage-limit guard | Sticky conservation at observed 90% session/weekly usage; requires positive reset observations to clear. |
| Portable installation | Dependency-free Node installer for skills, profiles and a marked global instruction block. |

Codex model identifiers, provider TOML, and permission-bypass profiles are not
portable Claude features. Claude's local worker is a native dispatcher invoking
a separate local inference surface, not a new native Claude model alias. The
legacy unrestricted shell delegation agent is not included in this bounded
worker package.

## Review findings and corrections

| Finding | Correction |
|---|---|
| Deployed sweep/probe wrappers required `.codex`; skill paths contained a username | Bundle the implementations and resolve paths relative to the skill/config root. |
| Portable installs could send a gateway token to a fixed LAN default | Make external gateway routing opt-in through an explicit base URL. |
| Merging skill directories retained obsolete scripts during upgrades | Stage and replace package-owned skill directories, preserving unrelated skills. |
| Fresh history reported target met; accounting could be duplicated by parent and worker | Explicit cold-start preference, one accounting owner and recent task IDs. |
| Policy reads wrote state and updates could race | Read-only `get`, validated migration, exclusive writer lock and atomic replacement. |
| Invalid Claude timeout key | Use documented `timeout` in milliseconds and validate it. |
| Classify/review accepted arbitrary model-returned JSON | Validate shape, types, label membership, unique complete indices and finding count. |
| Oversized aggregate input and provider response could exceed intended bounds | Aggregate input/source checks and bounded upstream body reads. |
| Non-loopback HTTP could start unauthenticated; loopback rebinding exposure | Require bearer authentication for non-loopback and validate loopback Host/Origin. |
| Verifiers could probe an invalid or substituted command/profile | Validate exact adapter, command, project layout and tool set before probes. |
| Same-named MCP server from another scope could be mistaken for the expected route | Check MCP project provenance in PreToolUse. |
| Cache signature missed validated limits; inherited auth differed between catalog and adapter | Include validated fields and resolve the effective environment consistently. |
| README described workstation-specific Muse configuration as the product | Describe configurable local LLM delegation; keep deployment details in setup docs. |
| Three vulnerable transitive dependencies | Update the lockfile to compatible patched versions; audit reports zero known vulnerabilities at review time. |

## Validation and boundaries

Validation results for this revision: Windows Node 24 / PowerShell 5.1 passed
56 tests; Linux Node 20.11.1 passed 48 with eight Windows/PowerShell checks
skipped because that runtime was absent. `npm audit` reported zero known
vulnerabilities, and `git diff --check` passed. Seven deployed routing/profile
files matched the pre-review snapshots byte-for-byte.

Regression coverage includes MCP tools, hook/configuration denial, native-routing
policy migration and conservation, isolated repeatable installation, accounting
lock recovery, and dispatcher failure handling. PowerShell tests use synthetic
HTTP servers for resident-ready, resident-busy, continuity fallback, both wire
protocols, Unicode packets, catalog-only/live-probe distinctions, and the legacy
policy CLI. No real LLM requests are required by the tests.

The installer is tested against temporary profiles. Publishing this repository
does not install it into an already-running production Claude session. Existing
production skills, agents and settings remain separate until explicitly installed.

Structural validation does not eliminate model hallucinations or prompt
injection. Parent validation remains required. MCP health checks are
non-generative and briefly cached; they do not promise runtime capacity.
Ganglion's sweep includes a completion probe but is not a reservation or queue.
The policy file serializes counters, not concurrent inference scheduling.

Claude behavior was checked against the official
[skills](https://code.claude.com/docs/en/skills),
[subagents](https://code.claude.com/docs/en/sub-agents),
[models](https://code.claude.com/docs/en/model-config), and
[hooks](https://code.claude.com/docs/en/hooks) documentation.
