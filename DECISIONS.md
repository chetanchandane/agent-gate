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
