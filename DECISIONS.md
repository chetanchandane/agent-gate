# Decisions log

One dated entry per deviation from the PRD or notable implementation choice.

## 2026-11-03 M0 scaffold

- Python 3.12 is provided through `uv` (the host had 3.11 and 3.13 available but
  not 3.12). `uv sync` pins an interpreter matching `requires-python = ">=3.12"`.
- Dev tooling (ruff, mypy, pytest, pytest-asyncio, pytest-cov) is declared in a
  `[dependency-groups]` dev group, installed by `uv sync`.
- `config/pricing.yaml` ships with placeholder prices. The PRD states the
  developer fills these from current Anthropic pricing; left as placeholders
  until a real run.
- CLI, graph, and gate modules are stubs at M0 so `mypy --strict` and the
  package entry point resolve. They are filled in M1 onward.

## 2026-11-04 Review follow-up

- M0 validation now checks the repository directories and YAML configuration
  files that exist at this milestone. It remains offline and does not require
  policies or eval cases that are scheduled for later milestones.
- CI validation is blocking. The previous `continue-on-error` behavior could
  report a green workflow while validation failed.
- The policy workflow does not receive API credentials. Until the runner and
  gate are implemented, it performs only offline validation. This avoids
  exposing a secret to arbitrary pull-request code through a workflow that
  checks out the pull-request head.
- Later gate implementation must preserve this boundary: policy evaluation in
  untrusted pull requests must not execute arbitrary repository code with
  secrets. Live evaluation requires a separately reviewed, least-privilege
  execution path.
- Later milestones must enforce safety invariants in application and tool code,
  protect holdout cases per case as well as in aggregate, use paired/statistical
  acceptance criteria, make spend accounting concurrency-safe, and pin model
  and pricing inputs for reproducible reports.

## 2026-11-04 M1 domain app

- The shared types `Policy` (in `app/policy_types.py`) and `RunResult` (in
  `app/runner_types.py`) live in `app/` so `gate/` can import them without
  `app/` ever importing `gate/`. `Policy.from_dir` reads and hashes a policy
  directory; the gate's loader and guard (M5) add validation on top rather than
  redefining the type.
- The seed types that build a case database live in `app/seed.py` rather than in
  the eval schema. The eval case schema (M2) will reuse them, keeping the agent
  independent of the gate.
- Node-to-model mapping follows the PRD: `intake`, `urgency`, and the chosen
  branch node plus `respond` each make one agent-model call; `route` is plain
  Python. A branch node writes a draft reply steered by its own policy file, and
  `respond` composes the final customer reply from that draft and calls
  `send_message`. This is four model calls per attempt at most.
- Spend accounting is concurrency-safe (an `asyncio.Lock` in `SpendTracker`).
  When `config/pricing.yaml` is missing, per-call cost is reported as unknown
  (`None`) and the dollar cap is enforced against a conservative token-based
  estimate using a documented fallback rate in `app/llm.py`. This honors the
  PRD rule that the cap still functions without a pricing file.
- Only `app/llm.py` imports the Anthropic access path (`langchain_anthropic`),
  and it does so lazily inside the live client so offline tests and `mypy` never
  need the live dependency resolved. All non-live tests use `FakeLLM`.
- Tracing uses the OpenTelemetry SDK with a custom JSONL span exporter per
  attempt. A null tracer (no exporter) is provided so offline tests create real
  spans without writing files.
- The `run` CLI command and end-to-end live acceptance
  (`agent-gate run --policy v1 --case happy_path-001 --k 1`) depend on the case
  loader (M2) and the runner (M3). M1 proves the graph end to end offline with
  `FakeLLM` integration tests (happy path, emergency, injection) instead.

## 2026-11-04 Emergency routing stays policy-driven (revert of cd2f5cf)

- A commit briefly made `route()` escalate whenever `detect_emergency(message)`
  matched, in addition to the model's urgency label. That was reverted.
- Reason: `app/rules.py` is the graders' ground-truth oracle (PRD section 4).
  Calling it from the agent's routing makes every emergency case pass by
  construction, collapses the agent/grader separation for that category, and
  removes the deliberate v1 urgency weakness the improvement loop is meant to
  fix through policy edits. It also added a broad over-escalation path that no
  policy edit could correct.
- Decision: emergency routing stays driven by the model's urgency label (the
  node steered by `urgency.md`). Deterministic emergency detection remains
  available to graders only, never to the agent.
- A production-style deterministic safety backstop may be worthwhile later, but
  only as an explicit PRD deviation recorded here, using an independently
  designed detector rather than the graders' oracle, so grading stays an
  independent check and the emergency category keeps policy-driven headroom.

## 2026-11-04 M2 cases and graders

- The eval case schema lives in `evals/schema.py` and reuses `app.seed.Seed`, so
  cases and the agent share one seed type and the agent never depends on the
  gate. The set (dev, safeguard, holdout) is defined only by the directory, per
  the PRD, so there is no `set` field. Every case model uses `extra="forbid"` so
  a typo in a case file fails validation instead of being silently ignored.
- The 70 starter cases are split 30 dev / 20 safeguard / 20 holdout. Happy path
  and emergency cases dominate safeguard; ambiguity, injection, and slot
  conflicts dominate dev and holdout. Emergencies, prompt injections, and
  pricing pressure are marked `critical`; each critical case carries a
  `must_not_call` or `db` expectation so it can never pass by luck, and no
  critical case has a judge check. Both invariants are enforced in the schema
  and again in `tests/test_cases_valid.py`.
- `gate/graders.py` is pure (no network, files, or clock). It grades a
  `RunResult` against a case: route, `must_call` (a successful matching call,
  `args_match` as a subset), `must_not_call` (any call, even a failed one,
  counts as a violation), `db` row counts, and reply regex, all combined with
  AND. The judge is never called from here; the grader only returns the judge
  checks that are eligible to run (deterministic checks all passed), and
  `parse_judge_response` reads the judge verdict. This keeps the safety-critical
  grading deterministic and the module unit-testable offline.
- Database counts are read from `RunResult.db_diff` using the keys the DB
  snapshot already produces (`bookings`, `escalations`, `messages`). The M3
  runner will populate `db_diff` from `snapshot()`; graders only read it.
- `agent-gate validate` now loads every case (failing on an invalid file or a
  duplicate id) and loads the active policy, in addition to the earlier
  directory and config checks.
- Set sizes are asserted at exactly 30 / 20 / 20 in the case test. The M3
  baseline step promotes weak safeguard cases into dev, which will change these
  counts; that test will be updated when promotion lands, and the change noted
  here.
- Housekeeping: removed a stray Finder-duplicated editable install file
  (`_editable_impl_agent_gate 2.pth`) that shadowed the real one and broke the
  `agent-gate` entry point. The same duplication produced the untracked
  `.coverage 2` file; both are build or tool artifacts, not source.
- Graders now fail when an expected database table is absent from `db_diff`,
  rather than treating missing evidence as zero rows. Reply regexes are
  validated when cases load, and judge responses enforce the exact documented
  boolean-and-string JSON shape.

## 2026-11-04 M3 runner, metrics, and cache

- `gate/metrics.py` is pure (no network, files, or clock). It defines the cached
  unit `ScoredAttempt` (a `RunResult` plus its grade and any judge outcomes) and
  computes a `SetResult`: pass rate as the mean of per-case scores, a Wilson 95
  percent interval on the attempt level, flaky cases, cost, p50/p95 latency,
  mean tool calls, and a failure histogram. Raw counts sit beside every rate.
- `gate/runner.py` does the impure work. Each attempt builds a fresh in-memory
  database from the case seed, runs the agent graph (via `build_graph`, not
  `arun`, so the runner owns the root span and the trace id), grades
  deterministically, and runs judge checks only when the deterministic checks
  pass. `db_diff` is the post-run snapshot. Concurrency is a semaphore sized by
  `MAX_CONCURRENCY`; a per-attempt timeout yields a failed attempt with reason
  `timeout`; `BudgetExceeded` and `LLMError` (an infrastructure failure after
  retries) propagate and abort the run rather than counting as policy failures.
- Per-attempt tokens and cost are captured by wrapping the run's LLM client in a
  small counting proxy and passing a `replace`d context to the graph, so the
  shared spend tracker still enforces the global cap while each `RunResult` gets
  its own usage. Judge calls go through the same proxy, so their cost is
  attributed to the attempt and counts toward the cap.
- `gate/cache.py` caches each scored attempt in `.cache/runs.jsonl`, keyed by
  policy content hash, case id, attempt, agent model, and a fingerprint of all
  `app/` code (plus `schema.sql`). A cache hit does no LLM work, so a repeated
  baseline costs zero. The key deliberately excludes case content (per the PRD);
  editing a case needs `--no-cache` or a cleared cache, which the runner and
  later milestones will expose.
- The cache stores the full scored attempt, judge verdicts included, so a repeat
  run reproduces grades without re-calling the judge. The trade-off is that a
  change to a case's deterministic expectations is not reflected until the cache
  entry is regenerated; this matches the PRD cache key and is acceptable because
  cases are frozen during a run.
- `gate/driver.py` is the live glue for the `run` and `baseline` commands:
  builds the live context, runs sets, prints `rich` tables, and performs the
  safeguard promotion (any safeguard case below 95 percent moves to `evals/dev/`).
  The orchestration takes an already-built context so it is unit-tested offline
  with `FakeLLM`; only the thin live-context builder is exercised solely in real
  runs.
- The live `agent-gate baseline` run was not executed in this milestone: it
  needs a real API key, spends against the cap, and mutates the eval sets via
  promotion. Per the PRD cost discipline, that run is left for the user to start
  deliberately. The runner, metrics, cache, promotion, and both commands are
  proven offline with `FakeLLM` integration tests (happy path, emergency, judge,
  repeated k, and a cache hit that adds no spend).
- Because the live baseline has not run, the eval sets are still 30 / 20 / 20
  and `tests/test_cases_valid.py` still asserts those sizes. The size assertion
  will be revisited after the first real baseline promotes any weak cases.
