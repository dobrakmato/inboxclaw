"""CLI command for source health reporting."""

import json
import os
from typing import Optional

import click
import httpx

from src.cli import cli
from src.config import load_config
from src.utils.paths import get_project_root


@cli.command()
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
@click.option("--json", "as_json", is_flag=True, help="Print the endpoint response as JSON.")
@click.option("--timeout", default=5.0, type=float, show_default=True, help="HTTP request timeout in seconds.")
def healthcheck(config_path: Optional[str], as_json: bool, timeout: float) -> None:
    """Report the live health self-assessment of every configured source."""
    if config_path is None:
        config_path = "config.yaml" if os.path.exists("config.yaml") else str(get_project_root() / "config.yaml")

    try:
        config = load_config(config_path)
    except Exception as error:
        click.secho(f"Configuration error: {error}", fg="red", err=True)
        raise click.exceptions.Exit(2)

    host = "127.0.0.1" if config.server.host == "0.0.0.0" else config.server.host
    url = f"http://{host}:{config.server.port}/healthcheck"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url)
        payload = response.json()
    except Exception as error:
        click.secho(f"Health endpoint {url} is unreachable: {error}", fg="red", err=True)
        raise click.exceptions.Exit(2)

    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.secho("=== Inboxclaw Source Health ===", bold=True)
        for source in payload.get("sources", []):
            status = source.get("status", "starting")
            pending = source.get("pending_failure")
            if pending:
                click.secho(
                    f" WARNING  {source['name']} ({source['type']}): "
                    f"{pending.get('code', 'error')} - {pending.get('message', '')} "
                    f"({pending.get('consecutive_failures', 1)}/"
                    f"{pending.get('required_failures', 2)} failed checks)",
                    fg="yellow",
                )
            elif status == "healthy":
                click.secho(f" OK       {source['name']} ({source['type']})", fg="green")
            elif status == "unhealthy":
                code = source.get("code") or "error"
                click.secho(
                    f" ERROR    {source['name']} ({source['type']}): {code} - {source.get('message', '')}",
                    fg="red",
                )
                if source.get("action"):
                    click.echo(f"          Action: {source['action']}")
            else:
                click.secho(f" STARTING {source['name']} ({source['type']}): {source.get('message', '')}", fg="yellow")
        click.echo(f"Overall: {payload.get('status', 'unknown')}")

    status = payload.get("status")
    if status == "unhealthy" or response.status_code == 503:
        raise click.exceptions.Exit(1)
    if status != "healthy":
        raise click.exceptions.Exit(2)
