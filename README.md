# agent-gate

Agent changes must earn acceptance through evidence. An improver LLM proposes
edits to versioned policy files, and a deterministic gate accepts an edit only
when it improves a dev set, breaks nothing in a protected safeguard set, and
holds on a hidden holdout set.

The full pipeline is implemented: domain agent, eval sets, graders, runner,
metrics, cache, the acceptance gate, the improver loop, hand written adversarial
candidates, reports, and CI. Everything except the live LLM runs is exercised
offline (`make check`). The headline numbers below are produced by a live
`agent-gate report` run, which needs an API key and spends against the cap, so
they are generated deliberately rather than committed.

## Quickstart

```bash
make install           # uv sync (creates .venv, installs deps)
cp .env.example .env   # then set ANTHROPIC_API_KEY
make check             # ruff, mypy, offline pytest (no API key needed)
```

Then, to produce results (uses the API, under 15 USD, enforced by a hard cap):

```bash
make demo              # validate, baseline, improve, adversarial, report, cost
```

`make demo` writes `reports/RESULTS.md` (paste ready) and `reports/report.html`
(static, opens offline). See `docs/DEMO.md` for the 90 second walkthrough.

## Commands

| Command | What it does |
| --- | --- |
| `agent-gate validate` | Validate every case file, policy dir, and config. No API calls |
| `agent-gate run --policy v1 --set dev [--k 3] [--case ID]` | Run one set, print a summary |
| `agent-gate baseline` | Run `v1` on all sets with `k = 5`, flag weak safeguard cases |
| `agent-gate improve --rounds 6 [--git-commit]` | The main loop: propose, guard, run, gate, accept or reject, promote |
| `agent-gate gate --candidate PATH [--baseline-dir PATH]` | Gate one candidate dir against a baseline. Exit 0 accepted, 1 rejected, 2 error. Used by CI |
| `agent-gate adversarial` | Run the hand written adversarial candidates through the gate |
| `agent-gate report [--out reports/]` | Evaluate first and latest policy on the frozen benchmark, write the reports |
| `agent-gate trace TRACE_ID` | Print the span tree with durations for one attempt |
| `agent-gate cost` | Show spend recorded in the run cache and the remaining budget |

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

## Continuous integration

- `ci.yml` runs on every push and pull request: `uv sync`, `ruff check`,
  `ruff format --check`, `mypy`, offline `pytest`, and `agent-gate validate`.
  It never needs an API key.
- `gate.yml` runs on pull requests that change `policies/**`. It checks out the
  immutable base revision as the executable checkout and the PR head only as
  policy input, then runs `agent-gate gate` against the base policy with
  `MAX_SPEND_USD=5`. It posts the R1 to R4 decision table as a PR comment
  (updated on reruns), and fails the job if the candidate is rejected. It is
  skipped for forks, which cannot read the API key secret.

## Reports

`agent-gate report` writes two files from the frozen benchmark
(`evals/benchmark.lock.yaml`):

- `reports/RESULTS.md`, paste ready for this README.
- `reports/report.html`, a single static file with no JavaScript framework.

Both contain the first vs latest headline, version history, the rejected
candidates table, per category pass rates, cost and latency, flaky cases, and
the limitations below.

## Limitations

Small sample sizes, a single domain, mock tools, an LLM judge used only for
tone, adversarial candidates written by hand, and results that vary between
runs. See the generated report for the full, honest list.
