# Decision provider and offline benchmark contract

Checked against [OpenAI's Decisions guide](https://developers.openai.com/api/docs/guides/decisions)
and [create reference](https://developers.openai.com/api/reference/resources/decisions/methods/create)
on 2026-10-17. The adapter uses a single choice question at the fixed HTTPS
`/v1/decisions` endpoint, with `gpt-6-luna` as evaluator. Returned confidence and
choice probabilities describe the selection, not downstream correctness.
The native candidates are independently confirmed Claude routes.

Rules retain the parent's eligible safe route. Decisions is default-off and
requires explicit API consent plus an existing credential. Admission applies
model/effort overrides, native confirmation, local readiness, conservation and
candidate budget ceilings before provider selection. A single remaining choice
does not need a paid evaluation. Malformed responses, refusal, unknown choices,
low confidence, timeout and network errors retain eligible rules fallback and
record the actual reason. Timed-out requests are not automatically retried:
the service may already have received and billed them.

The full evaluator input token count is not known before the request. Therefore
**setting a hard evaluator input-token or dollar cap blocks evaluation**, rather
than estimating a count and risking an overrun. Candidate execution bounds are
separate: missing required bounds fail admission; the explicit local adapter
enforces its output-token limit. There is no native execution adapter in this
package, so a candidate's native bound remains a declared planning constraint.

The guide's current base input rate is used only for a labeled estimate in the
ledger. It is not measured billing, a subscription price, or a guarantee after
regional/long-context adjustments. API access and account availability have not
been live-tested. Secrets and provider error bodies do not enter decision
records. Raw task summaries are not stored in the ledger.

## Honest offline comparison

```sh
python -B scripts/routing_benchmark.py plan assets/benchmarks/suite.synthetic.json
python -B scripts/routing_benchmark.py compare assets/benchmarks/suite.synthetic.json assets/benchmarks/outcomes.synthetic.json
```

These commands validate fixtures and compute arithmetic without provider,
worker, credential or home-state access. The checked-in outcomes are invented
demonstrations. They are not results from Claude, Decisions or a local model.
The report explicitly labels this evidence `synthetic_demonstration_only`.

For real evaluation, freeze your workload and rubric before collecting outcomes.
Supply matched case/trial results for rules, always-Sonnet and Decisions; preserve
development/holdout splits, failed trials, unknown billing and latency including
evaluation, tools and retries. The scaffold validates schemas and paired blocks;
it cannot attest supplied outcomes, execute a rubric, establish statistical
significance, or prove quality from confidence. Missing/excluded pairs are
reported rather than treated as passes. Measured cost-per-pass stays unknown
when billing is incomplete. Never present synthetic savings as measured savings.
