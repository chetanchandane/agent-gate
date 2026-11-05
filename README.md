# agent-gate

Agent changes must earn acceptance through evidence. An improver LLM proposes
edits to versioned policy files, and a deterministic gate accepts an edit only
when it improves a dev set, breaks nothing in a protected safeguard set, and
holds on a hidden holdout set.

> Status: scaffold (M0). The headline results table, demo video link, loop
> diagram, and rejected-candidates table land as later milestones complete.

## Quickstart

```bash
make install          # uv sync (creates .venv, installs deps)
cp .env.example .env   # then set ANTHROPIC_API_KEY
make check             # ruff, mypy, offline pytest
```

Expected cost for a full demo run is under 15 USD (enforced by a hard cap).

## How the gate decides

| Rule | Condition to pass |
| --- | --- |
| R1 dev gain | dev pass rate improves by at least `MIN_DEV_GAIN` |
| R2 safeguard | no confirmed regression; overall safeguard pass rate holds |
| R3 holdout | holdout pass rate holds within `HOLDOUT_TOLERANCE` |
| R4 budgets | mean cost and p95 latency stay within their increase caps |

## Adversarial candidates

`gate/adversarial/` holds three candidate policies written by hand to test the
gate, not proposed by the improver. They are deliberately bad: always book the
earliest slot without asking for missing details, treat any message mentioning
water as an emergency, and a shortened `shared_rules.md` that drops the safety
guardrails. `agent-gate adversarial` runs them through the exact same gate as
the improve loop, labeled `source: adversarial`, and records a decision for
each. They never change the active policy and are kept out of the improver
acceptance statistics. If the gate accepts one, that outcome is reported
honestly rather than forced to a rejection by tuning thresholds.

## Repository layout

```text
app/      # the agent under test (graph, tools, llm wrapper, tracing)
gate/     # evaluation and improvement machinery (runner, graders, gate, improver, report)
policies/ # versioned instruction files, the only thing the improver may change
evals/    # dev, safeguard, holdout eval cases
config/   # gate.yaml, pricing.yaml
reports/  # decisions, history, RESULTS.md, report.html
docs/     # PRD.md
```

## Limitations

Small sample sizes, a single domain, mock tools, an LLM judge used only for
tone, adversarial candidates written by hand, and results that vary between
runs. See the generated report for the full, honest list.
