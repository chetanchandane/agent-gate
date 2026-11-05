.PHONY: install check validate baseline improve adversarial report cost demo

# agent-gate is invoked with --no-sync so a transient re-sync never drops the
# editable install mid-run (see the M5 note in DECISIONS.md).
AGENT_GATE = uv run --no-sync agent-gate

install:
	uv sync

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest --cov -m "not live"

validate:
	$(AGENT_GATE) validate

baseline:
	$(AGENT_GATE) baseline

improve:
	$(AGENT_GATE) improve --rounds 6

adversarial:
	$(AGENT_GATE) adversarial

report:
	$(AGENT_GATE) report

cost:
	$(AGENT_GATE) cost

# The full demo from a clean clone. validate is offline; the rest need
# ANTHROPIC_API_KEY and spend against the cap. See docs/DEMO.md.
demo: install validate baseline improve adversarial report cost
