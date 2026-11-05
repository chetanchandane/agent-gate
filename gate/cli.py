"""Single responsibility: the Typer CLI that is the product surface of agent-gate.

At M0 the commands are registered but not implemented. Each milestone fills in
its command. Running an unimplemented command exits with a clear message.
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml

from app.errors import ConfigError
from app.policy_types import Policy
from evals.schema import load_all

app = typer.Typer(
    name="agent-gate",
    help="A regression gate for changes to an LLM agent's behavior.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def validate() -> None:
    """Validate every case file, policy directory, and config file."""
    try:
        _validate_repository(Path.cwd())
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("Repository validation passed.")


@app.command()
def run(
    policy: str = "v1",
    set_: str = typer.Option("dev", "--set"),
    k: int = 3,
    case: str | None = None,
) -> None:
    """Run one eval set under one policy and print a summary table."""
    from rich.console import Console

    from gate import driver

    try:
        driver.run_one_set(Path.cwd(), Console(), policy, set_, k, case)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def gate(candidate: str, baseline_dir: str | None = None) -> None:
    """Evaluate one candidate directory against a baseline (used by CI)."""
    from rich.console import Console

    from app.errors import AgentGateError
    from gate import driver

    try:
        code = driver.run_gate_live(Path.cwd(), Console(), candidate, baseline_dir)
    except AgentGateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    raise typer.Exit(code)


@app.command()
def baseline() -> None:
    """Run v1 on all sets with k = 5 and flag weak safeguard cases."""
    from rich.console import Console

    from gate import driver

    try:
        driver.run_baseline(Path.cwd(), Console())
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def improve(rounds: int = 6, git_commit: bool = False) -> None:
    """The main loop: propose, guard, run, gate, accept or reject, promote."""
    from rich.console import Console

    from gate import driver

    try:
        driver.run_improve_live(Path.cwd(), Console(), rounds, git_commit)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def adversarial() -> None:
    """Run the hand written adversarial candidates through the gate."""
    from rich.console import Console

    from gate import driver

    try:
        driver.run_adversarial_live(Path.cwd(), Console())
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def report(out: str = "reports/") -> None:
    """Evaluate the first and latest policy on the frozen benchmark."""
    from rich.console import Console

    from gate import driver

    try:
        driver.run_report_live(Path.cwd(), Console(), Path(out))
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def trace(trace_id: str) -> None:
    """Print a span tree with durations for one attempt."""
    from rich.console import Console

    from gate import driver

    try:
        driver.print_trace(Path.cwd(), Console(), trace_id)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def cost() -> None:
    """Show total spend so far and the remaining budget."""
    from rich.console import Console

    from gate import driver

    driver.run_cost(Path.cwd(), Console())


def _validate_repository(root: Path) -> None:
    """Validate the repository inputs available at the current milestone."""
    required_directories = (
        "app",
        "config",
        "evals/dev",
        "evals/holdout",
        "evals/safeguard",
        "gate",
        "policies",
        "reports/decisions",
        "tests",
    )
    missing_directories = [
        directory for directory in required_directories if not (root / directory).is_dir()
    ]
    if missing_directories:
        raise ConfigError("Missing required directories: " + ", ".join(sorted(missing_directories)))

    yaml_files = (root / "config" / "gate.yaml", root / "config" / "pricing.yaml")
    for yaml_file in yaml_files:
        try:
            with yaml_file.open(encoding="utf-8") as handle:
                document = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Invalid YAML in {yaml_file}: {exc}") from exc
        if not isinstance(document, dict):
            raise ConfigError(f"Expected a mapping in {yaml_file}")

    _validate_cases(root)
    _validate_active_policy(root)
    from gate.driver import load_benchmark_sets

    load_benchmark_sets(
        root, {str(name): cases for name, cases in load_all(root / "evals").items()}
    )


def _validate_cases(root: Path) -> None:
    """Load every eval case, failing on an invalid file or a duplicate id."""
    sets = load_all(root / "evals")
    seen: dict[str, str] = {}
    for set_name, cases in sets.items():
        for case in cases:
            if case.id in seen:
                raise ConfigError(f"Duplicate case id {case.id} in {set_name} and {seen[case.id]}")
            seen[case.id] = set_name


def _validate_active_policy(root: Path) -> None:
    """Load the active policy named in policies/ACTIVE."""
    active_file = root / "policies" / "ACTIVE"
    try:
        version = active_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"Cannot read {active_file}: {exc}") from exc
    if not version:
        raise ConfigError(f"{active_file} is empty")
    policy_dir = root / "policies" / version
    if not policy_dir.is_dir():
        raise ConfigError(f"Active policy directory missing: {policy_dir}")
    Policy.from_dir(version, policy_dir)


if __name__ == "__main__":
    app()
