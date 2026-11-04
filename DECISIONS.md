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
