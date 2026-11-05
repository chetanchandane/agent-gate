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
- The cache key also includes the judge model and a fingerprint of the complete
  case definition. This prevents stale grades after judge configuration or case
  expectations change. Baseline sets run on one event loop so the shared,
  concurrency-safe spend tracker remains valid across all three sets.
- The optional policy commit stages only files produced by that accepted round.
  It never uses `git add -A`, so unrelated worktree changes and artifacts cannot
  be swept into an automated commit. Policy-guard word matching tokenizes
  punctuation consistently, and non-positive improve rounds are rejected.

## 2026-11-04 M4 gate

- `gate/gate.py` is pure (no network, file writes, or clock reads): every rule
  is a function of two `SetResult` bundles (active A and candidate C) plus the
  thresholds. The `created_at` timestamp and the R2 confirmation reruns are
  passed in by the caller rather than produced inside the module, so the gate is
  fully unit-testable with synthetic `SetResult`s.
- The R2 confirmation step reruns flagged cases, which is impure LLM work and
  therefore cannot live in the pure gate. The gate expresses the decision as a
  pure helper `confirmation_stands(a_failures, c_failures, severity)` and takes
  an optional `confirm` callback that supplies the rerun failure counts. When no
  callback is given, every flag is treated as confirmed (the conservative
  default), so a gate run without reruns never silently clears a regression. The
  live `improve` loop (M5) injects the real rerun callback.
- R2 needs each safeguard case's severity (critical cases regress on any drop,
  normal cases on a drop of two or more attempts), which `SetResult` does not
  carry. The gate takes a `severities` mapping of case id to severity instead of
  importing or re-reading the case files, keeping it pure and decoupled.
- R4 budgets are measured over the combined dev and safeguard attempts. Mean
  cost per attempt is the summed cost over the summed attempts of both sets; p95
  latency is recomputed from every attempt's `latency_ms` across both sets using
  the same percentile helper as the metrics module. When pricing is unknown
  (no pricing file) the cost budget is treated as satisfied and `cost_known` is
  recorded as false, so the decision shows the gap honestly rather than
  rejecting on a missing input.
- `GateConfig` is a Pydantic model with a `from_mapping` constructor. The gate
  does not read `config/gate.yaml` itself (that is a file read best kept in the
  impure CLI or driver layer); the caller parses the YAML and passes a mapping.
- `GateDecision` mirrors the PRD JSON record (candidate id, source, parent,
  decision, failed rules, per-rule results, warnings, cost, created_at) and adds
  a `summary_line()` for `reports/history.jsonl`. Writing the decision file, the
  history append, and the accept/reject promotion side effects are orchestration
  and land with the `improve` command in M5; M4 delivers only the pure decision.
- Table-driven tests in `tests/test_gate.py` cover R1 (gain, no-gain, equal,
  regression, exact-threshold), R2 (critical any-drop, normal one-drop tolerated,
  normal two-drop regressed, rate floor without per-case regression), the
  confirmation step (`noise_cleared` and a flag that stands, plus the margin
  table for both severities), R3 (single-case and small-drop-within-tolerance),
  R4 (within budget, cost over, latency over, unknown cost), the overfit warning,
  and `decide` end to end (clean accept, guard short-circuit, R1 rejection).

## 2026-11-05 M5 improver, loop, and policy guard

- `gate/policy_guard.py` is pure: it takes the candidate files, the parent
  files, the eval messages, and the holdout ids as plain data and returns a bare
  violation reason or None. It never reads the policy or case files itself, so it
  has no network, file, or clock access. The gate prefixes the reason with
  `policy_guard:` in the decision record. The change-size limit is a unified diff
  against the parent; the anti-overfitting check normalizes whitespace and case,
  then looks for any run of eight or more consecutive words from any eval message
  appearing verbatim in the concatenated policy text.
- `gate/improver_input.py` makes holdout leakage impossible by construction. The
  builder's parameters are the active policy, a dev `SetResult`, the dev cases,
  and safeguard summaries typed as `SafeguardStatus` (id and pass or fail only).
  There is no holdout parameter, so a holdout result is a type error rather than
  a runtime check. Failing dev cases are the lowest-scoring, at most two per
  category and eight in total, each with a compact trace drawn from one recorded
  failing attempt. `tests/test_no_holdout_leak.py` serializes the real improver
  prompt and asserts no holdout id or message snippet appears, and that safeguard
  entries expose nothing but id and passed.
- `gate/improver.py` keeps the pure parts (the `Proposal`/`Edit` models, JSON
  parsing with code-fence stripping, and `apply_edits`) separate from the single
  impure `propose` call. `apply_edits` rejects an unknown file, an unknown op, a
  `str_replace` whose `old` is missing or not unique, and an append or
  replace_file with no text, so a bad proposal never yields a half-applied
  candidate. `propose` retries once with the validation error fed back, then
  raises `InvalidProposal`; the loop records that as `invalid_proposal` without
  running the candidate.
- The improver system prompt lives in `gate/prompts/improver.md` as code, not
  policy, per section 8, and the improver input is serialized to the user message
  as JSON so the structure is unambiguous and contains only allowed fields.
- `gate/loop.py` is the impure orchestration behind `agent-gate improve`: per
  round it runs the active and candidate policies on all three sets (cached),
  builds the improver input, proposes, guards, gates, writes the decision record
  and history line, and either accepts (new version, manifest, ACTIVE, dev to
  safeguard promotion with a `promoted_in` meta block, optional git commit) or
  files the candidate under `policies/rejected/<id>/` with its decision. A
  rejected round's reason feeds the next round's input.
- The R2 confirmation reruns are async, but `gate.decide` calls `confirm`
  synchronously. The loop resolves this by first computing the flagged cases with
  `check_r2` (no confirm), then awaiting the reruns for each flag to build a
  `case_id -> (a_failures, c_failures)` map, and finally passing `decide` a
  synchronous lookup into that map. This keeps the gate pure and synchronous while
  the impure reruns happen in the loop, bypassing the cache as the PRD requires.
- As with the live `baseline`, the live `agent-gate improve` run was not executed
  in this milestone: it needs a real API key and spends against the cap. The loop
  side effects and pure helpers (version bump, promotion with meta, accept and
  reject file writes, config load, the no-run rejection record) are proven offline
  in `tests/test_loop.py`; the full live round is left for the user to start
  deliberately. This is why `gate/loop.py` line coverage is lower than the other
  gate modules: the end-to-end `run_round` path runs the agent graph and the
  improver live.
- Tooling note: `uv run agent-gate ...` sometimes re-syncs the environment and
  transiently drops the editable install, giving `ModuleNotFoundError: gate`.
  `uv run --no-sync agent-gate ...` or a reinstall resolves it. This is the same
  editable-install gremlin recorded in the M2 entry, not a code fault; the CLI
  imports cleanly under `uv run python -c "from gate.cli import app"`.

## 2026-11-05 M6 adversarial candidates

- The three hand written candidates live in `gate/adversarial/` as JSON files,
  each an id, a human label, an expectation, and a `Proposal` shaped set of
  edits (the same `Edit` model the improver uses). They apply to the active
  policy with `append` or `replace_file` ops rather than `str_replace`, so they
  stay robust against small wording changes in v1.
- PRD section 8 describes candidate three as a shortened `shared_rules.md` that
  removes the instruction to ignore instructions found inside customer messages.
  Policy v1's `shared_rules.md` has no such explicit line. The candidate instead
  removes the closest existing safety guardrails (the tool grounding rule and
  the no price rule), leaving only the role and a brevity line. This preserves
  the PRD intent (a shortened shared_rules that drops a safety rule, expected to
  regress injection and pricing cases) and is recorded here per the hard rule on
  deviations. Policy v1, thresholds, graders, and holdout are unchanged.
- The runner module is `gate/adversary.py`, not `gate/adversarial.py`, because a
  module of the same name as the `gate/adversarial/` candidates directory would
  collide on import. The CLI command stays `agent-gate adversarial` and the
  candidates directory keeps its PRD name.
- The gate running core (policy guard, run all three sets, the R2 confirmation
  reruns, and `gate.decide`, then write the decision record) was extracted from
  `loop.run_round` into `loop.evaluate_candidate`, so the improve loop and the
  adversarial command use the identical gate path. `evaluate_candidate` performs
  no accept, reject, or promotion side effects; those stay in `run_round`. The
  adversarial command records decisions with `source="adversarial"` and never
  mutates ACTIVE or promotes, even if a candidate is accepted, which keeps the
  candidates out of the improver acceptance statistics.
- The always-book adversarial candidate targets `intake.md`, not only
  `schedule.md`, because deterministic routing sends requests with missing
  fields to `clarify` before the schedule node. The candidate must encourage
  fabricated intake values to exercise the intended regression.
- An accepted adversarial candidate is printed in red (the gate let a bad policy
  through) and a rejected one in green (the gate did its job), the inverse of the
  improve loop's coloring.
- As with the live `baseline` and `improve` runs, the live `agent-gate
  adversarial` run was not executed in this milestone: it needs a real API key
  and spends against the cap. Offline, the FakeLLM ignores policy text, so a
  candidate behaves identically to the active policy and the LLM driven
  regression cannot be shown without the live run. `tests/test_adversarial.py`
  therefore proves the mechanics and the labeling (three decisions recorded,
  each `source="adversarial"`, ACTIVE untouched, no version promoted) and leaves
  the honest per candidate outcomes to the deliberate live run.

## 2026-11-05 M7 reports

- `gate/report.py` is pure except for `write_reports`. The assembly helpers
  (`build_headline`, `build_categories`, `build_rejected`, `build_flaky`, and
  the `build_report_data` composer) take `SetResult` bundles, gate decisions,
  and a case to category map as plain data and return a `ReportData` model. The
  two renderers turn that model into Markdown and a single static HTML file
  through Jinja2 templates in `gate/templates/`. No network, no clock inside the
  pure path; the timestamp is passed in by the driver.
- The report covers all seven PRD section 10 blocks: the first vs latest
  headline with counts and Wilson intervals, the version history, the rejected
  candidates table (improver and adversarial together, with confirmed regressed
  case ids), per category pass rates with inline SVG bars in the HTML, cost and
  latency, flaky cases, and the auto included limitations. The HTML is one file
  with no JavaScript framework and no external assets, so it opens offline.
- Per category rates aggregate each case's score across all three sets, grouped
  by the case `category`, before (first policy) and after (latest policy). This
  is a readable before and after view rather than a per set breakdown, matching
  the PRD's single per category table.
- Spend by component (agent, improver, judge, confirmation reruns) is not
  separately tracked: the spend tracker records a single global total and each
  attempt's cost already folds in the agent and judge calls. The cost section
  therefore reports the honest total and per version p50 and p95 latency, and
  leaves the component breakdown empty rather than inventing a split. Adding
  per component attribution would require tagging every LLM call with its role
  in `app/llm.py`; recorded here as a known limitation to revisit if needed.
- Version history is built from the policy manifests (version, source, note)
  with dev, safeguard, holdout, mean cost per attempt, and p95 latency filled in
  for the two benchmarked versions (first and latest). Intermediate accepted
  versions show n/a for those numbers because the frozen benchmark only
  evaluates the first and latest policy, per the PRD `report` command.
- As with `baseline`, `improve`, and `adversarial`, the live `agent-gate report`
  run was not executed here: it needs a real API key and spends against the cap.
  The pure assembly and both renderers are proven offline in
  `tests/test_report.py` from fixture `SetResult`s and a fixture rejected
  decision, which assert all seven sections, the inline SVG bars, the single
  file no framework HTML, and graceful empty rejected and flaky tables.

## 2026-11-05 M7 review fixes

- The report previously evaluated the current eval directories instead of a
  frozen benchmark. Added `evals/benchmark.lock.yaml` and made reporting and
  repository validation resolve cases through that lock, with duplicate and
  missing-id checks.
- The report previously evaluated first and latest policies through separate
  event loops while reusing one spend tracker. Both evaluations now run on one
  event loop, preserving the async-lock invariant established in M3.
- Unknown-cost attempts were included as zero in version mean-cost output.
  Version cost now renders as unknown unless every attempt has known pricing.
- Spend component attribution was previously empty. LLM calls now record agent,
  judge, and improver costs in the shared spend tracker, and reports render the
  recorded component totals.
