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
