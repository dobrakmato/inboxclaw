from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from src.cli import cli
from src.config import Config


def _config() -> Config:
    return Config(
        server={"host": "0.0.0.0", "port": 8000},
        database={"db_path": ":memory:"},
        sources={},
        sink={},
    )


def _response(status_code: int, status: str) -> MagicMock:
    response = MagicMock(status_code=status_code)
    response.json.return_value = {
        "status": status,
        "sources": [
            {
                "name": "gmail_primary",
                "type": "gmail",
                "status": status,
                "code": "authentication" if status == "unhealthy" else None,
                "message": "Token revoked" if status == "unhealthy" else "Operational",
                "action": None,
                "pending_failure": None,
            }
        ],
        "failures": [],
    }
    return response


def test_healthcheck_command_healthy():
    runner = CliRunner()
    with patch("src.cli.commands.healthcheck.load_config", return_value=_config()), patch(
        "src.cli.commands.healthcheck.httpx.Client"
    ) as client_class:
        client_class.return_value.__enter__.return_value.get.return_value = _response(200, "healthy")
        result = runner.invoke(cli, ["healthcheck"])

    assert result.exit_code == 0
    assert "OK       gmail_primary" in result.output
    assert "Overall: healthy" in result.output


def test_healthcheck_command_unhealthy_exit_code():
    runner = CliRunner()
    with patch("src.cli.commands.healthcheck.load_config", return_value=_config()), patch(
        "src.cli.commands.healthcheck.httpx.Client"
    ) as client_class:
        client_class.return_value.__enter__.return_value.get.return_value = _response(503, "unhealthy")
        result = runner.invoke(cli, ["healthcheck"])

    assert result.exit_code == 1
    assert "ERROR    gmail_primary" in result.output


def test_healthcheck_command_shows_unconfirmed_failure_as_warning():
    response = _response(200, "healthy")
    response.json.return_value["sources"][0]["pending_failure"] = {
        "code": "timeout",
        "message": "The request timed out.",
        "consecutive_failures": 1,
        "required_failures": 2,
    }
    runner = CliRunner()
    with patch("src.cli.commands.healthcheck.load_config", return_value=_config()), patch(
        "src.cli.commands.healthcheck.httpx.Client"
    ) as client_class:
        client_class.return_value.__enter__.return_value.get.return_value = response
        result = runner.invoke(cli, ["healthcheck"])

    assert result.exit_code == 0
    assert "WARNING  gmail_primary" in result.output
    assert "1/2 failed checks" in result.output
