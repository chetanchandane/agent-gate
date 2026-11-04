"""Single responsibility: the Typer CLI that is the product surface of agent-gate.

At M0 the commands are registered but not implemented. Each milestone fills in
its command. Running an unimplemented command exits with a clear message.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="agent-gate",
    help="A regression gate for changes to an LLM agent's behavior.",
    no_args_is_help=True,
    add_completion=False,
)

_NOT_YET = "This command is not implemented yet. It lands in a later milestone."


@app.command()
def validate() -> None:
    """Validate every case file, policy directory, and config file."""
    raise typer.Exit(_fail())


@app.command()
def run(
    policy: str = "v1",
    set_: str = typer.Option("dev", "--set"),
    k: int = 3,
    case: str | None = None,
) -> None:
    """Run one eval set under one policy and print a summary table."""
    raise typer.Exit(_fail())


@app.command()
def gate(candidate: str, baseline_dir: str | None = None) -> None:
    """Evaluate one candidate directory against a baseline (used by CI)."""
    raise typer.Exit(_fail())


@app.command()
def baseline() -> None:
    """Run v1 on all sets with k = 5 and flag weak safeguard cases."""
    raise typer.Exit(_fail())


@app.command()
def improve(rounds: int = 6, git_commit: bool = False) -> None:
    """The main loop: propose, guard, run, gate, accept or reject, promote."""
    raise typer.Exit(_fail())


@app.command()
def adversarial() -> None:
    """Run the hand written adversarial candidates through the gate."""
    raise typer.Exit(_fail())


@app.command()
def report(out: str = "reports/") -> None:
    """Evaluate the first and latest policy on the frozen benchmark."""
    raise typer.Exit(_fail())


@app.command()
def trace(trace_id: str) -> None:
    """Print a span tree with durations for one attempt."""
    raise typer.Exit(_fail())


@app.command()
def cost() -> None:
    """Show total spend so far and the remaining budget."""
    raise typer.Exit(_fail())


def _fail() -> int:
    typer.echo(_NOT_YET, err=True)
    return 1


if __name__ == "__main__":
    app()
