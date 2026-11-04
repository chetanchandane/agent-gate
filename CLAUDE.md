# agent-gate

Every change to an LLM agent's behavior must pass a regression gate before it
is accepted. An improver LLM proposes edits to versioned policy files. A runner
executes the agent against dev, safeguard, and holdout eval sets. A gate accepts
an edit only if it improves dev, breaks nothing in the safeguard set, and holds
on holdout. Accepted fixes promote their failing cases into the safeguard set.

The domain under test is a fictional home-services booking and dispatch agent
(Brightline Home Services: HVAC, plumbing, electrical).

## Stack

| Concern | Choice | Notes |
| --- | --- | --- |
| Language | Python 3.12 | Type hints everywhere, `mypy --strict` clean on `app/` and `gate/` |
| Package manager | `uv` | `pyproject.toml`, lockfile committed |
| Agent orchestration | LangGraph | One compiled graph, typed state |
| LLM access | `anthropic` SDK via `langchain-anthropic` | Models set by env vars, never hard coded |
| Data models | Pydantic v2 | All YAML and JSON validated on load |
| CLI | Typer | Entry point `agent-gate` |
| Test framework | pytest | Plus `pytest-asyncio` if async is used |
| Persistence | SQLite via stdlib `sqlite3` | Mock business data and run results |
| Tracing | OpenTelemetry SDK | Console and JSONL file exporters |
| Config | `pydantic-settings` + `.env` | `.env.example` committed |
| Lint and format | `ruff` | Enforced in CI |
| CI | GitHub Actions | Lint, types, unit tests, and the gate |
| Report | Jinja2 | Static HTML plus Markdown output |

## Conventions

- Pure functions where possible. Side effects (LLM calls, DB writes, file
  writes) live in thin modules.
- Every LLM call goes through one wrapper, `app/llm.py`: retries, token and cost
  accounting, the global spend cap, and tracing. No other module imports the
  Anthropic SDK directly.
- No global mutable state. Pass a `RunContext` object explicitly.
- `gate/` may import `app/`. `app/` never imports `gate/`.
- `gate/gate.py`, `gate/metrics.py`, `gate/graders.py` are pure: no network, no
  file writes, no clock reads.
- Every module has a docstring stating its single responsibility.
- Errors: define `AgentGateError` and subclasses. No bare `except`.
- Determinism: the mock database is seeded from a fixed seed per case. Only the
  LLM is non-deterministic.
- Secrets: only `ANTHROPIC_API_KEY` is required. Never log it. `.env` is
  gitignored.
- No em dashes in any user facing text, docs, or code comments.

## Commands

| Command | What it does |
| --- | --- |
| `agent-gate validate` | Validate every case file, policy dir, and config. No API calls |
| `agent-gate run --policy v1 --set dev [--k 3] [--case ID]` | Run one set, print summary |
| `agent-gate baseline` | Run `v1` on all sets with `k = 5`, flag safeguard cases below 95% |
| `agent-gate improve --rounds 6 [--git-commit]` | The main loop |
| `agent-gate gate --candidate PATH [--baseline-dir PATH]` | Gate one candidate (CI) |
| `agent-gate adversarial` | Run hand written adversarial candidates through the gate |
| `agent-gate report [--out reports/]` | Evaluate first and latest policy, write reports |
| `agent-gate trace TRACE_ID` | Print a span tree for one attempt |
| `agent-gate cost` | Show total spend and remaining budget |

## Hard rule

Never edit holdout cases, policy `v1`, gate thresholds, or graders after viewing
holdout results. If something must change, record it in `DECISIONS.md` with the
reason.
