.PHONY: install check validate baseline improve adversarial report demo

install:
	uv sync

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest --cov -m "not live"

validate:
	uv run agent-gate validate

baseline:
	uv run agent-gate baseline

improve:
	uv run agent-gate improve --rounds 6

adversarial:
	uv run agent-gate adversarial

report:
	uv run agent-gate report

demo: validate baseline improve adversarial report
	uv run agent-gate cost
