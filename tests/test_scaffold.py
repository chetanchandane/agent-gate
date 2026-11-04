"""M0 smoke tests: the package imports and the CLI is wired up."""

from typer.testing import CliRunner

from app.errors import AgentGateError, BudgetExceeded
from gate.cli import app

runner = CliRunner()


def test_error_hierarchy() -> None:
    assert issubclass(BudgetExceeded, AgentGateError)


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("validate", "baseline", "improve", "gate", "report", "cost"):
        assert command in result.output


def test_unimplemented_command_exits_nonzero() -> None:
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 1
