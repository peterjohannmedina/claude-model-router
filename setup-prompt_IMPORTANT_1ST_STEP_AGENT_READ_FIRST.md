# Agent installation prompt

Copy this prompt into the Claude Code session that should install the package:

> Install Claude Model Router from this checkout for my user, using the supported
> `scripts/bootstrap.ps1` on Windows or `scripts/bootstrap.sh` on macOS/Linux.
> Read README.md and the current skill before acting. Inspect dependencies and
> report the intended paths. Install the complete versioned package, then run
> doctor/status. Preserve my settings, instructions, credentials, history and
> manually pinned model. Do not activate paid providers, telemetry hooks, login
> startup, or model switching merely because I requested installation. Offer
> explicit dashboard startup and shortcut choices; use `open` to reopen a running
> dashboard. Confirm native model eligibility through my supported Claude Code
> `/model` picker and import only the models/efforts I confirm. API discovery
> cannot prove my subscription entitlement. Do not change shell policy or ACLs to
> make setup pass. If blocked, report the actual failed check and stop that step.

Installation changes files under the selected Claude config root. Start a new
Claude Code session to discover updated skills and agent profiles. Dashboard
files being installed does not prove the current session has loaded the skill,
that a browser opened, or that a provider was evaluated.
