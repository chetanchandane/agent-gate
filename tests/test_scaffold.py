"""M0 smoke tests: the package imports and the CLI is wired up."""

from pathlib import Path

from typer.testing import CliRunner

from app.errors import AgentGateError, BudgetExceeded
from gate.cli import _validate_repository, app

runner = CliRunner()


def test_error_hierarchy() -> None:
    assert issubclass(BudgetExceeded, AgentGateError)


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("validate", "baseline", "improve", "gate", "report", "cost"):
        assert command in result.output


def test_validate_command_exits_successfully() -> None:
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert "validation passed" in result.output.lower()


def test_repository_validation_accepts_scaffold() -> None:
    _validate_repository(Path(__file__).parents[1])
