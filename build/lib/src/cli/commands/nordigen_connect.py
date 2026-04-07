"""
CLI command for connecting a bank account via GoCardless Bank Account Data (Nordigen).

Run once per bank account you want to monitor. The command walks you through:
  1. Authenticating with GoCardless using your API credentials.
  2. Picking your bank from a list.
  3. Opening a browser link to grant consent.
  4. Writing the resulting account ID(s) into your config.yaml as separate sources.
"""

import asyncio
import uuid
import webbrowser
from pathlib import Path
from typing import List, Optional

import click
from ruamel.yaml import YAML

from src.cli import cli
from src.utils.paths import get_project_root
from src.utils.nordigen_client import (
    Institution,
    bootstrap_refresh_token,
    create_requisition,
    get_requisition,
    list_institutions,
    refresh_access_token,
)


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------

@cli.group()
def nordigen():
    """GoCardless Bank Account Data (Nordigen) commands."""
    pass


# ---------------------------------------------------------------------------
# nordigen auth  — mint and display a refresh token
# ---------------------------------------------------------------------------

@nordigen.command()
@click.option("--secret-id", envvar="NORDIGEN_SECRET_ID", required=True,
              help="Your GoCardless secret_id (or set NORDIGEN_SECRET_ID env var).")
@click.option("--secret-key", envvar="NORDIGEN_SECRET_KEY", required=True,
              help="Your GoCardless secret_key (or set NORDIGEN_SECRET_KEY env var).")
def auth(secret_id: str, secret_key: str):
    """
    Exchange your GoCardless API credentials for a long-lived refresh token.

    Run this once to obtain the refresh token you need to put in your
    NORDIGEN_REFRESH_TOKEN environment variable (or .env file).
    """
    click.echo("\nContacting GoCardless to mint a refresh token…")
    try:
        refresh_token, refresh_expires, _access, _access_expires = asyncio.run(
            bootstrap_refresh_token(secret_id, secret_key)
        )
    except Exception as exc:
        raise click.ClickException(f"Failed to obtain refresh token: {exc}") from exc

    refresh_days = refresh_expires // 86400
    click.echo(f"\n✓ Refresh token obtained (valid for ~{refresh_days} days):\n")
    click.secho(f"  {refresh_token}", fg="green", bold=True)
    click.echo(
        "\nAdd this to your .env file:\n"
        f"  NORDIGEN_REFRESH_TOKEN={refresh_token}\n"
    )


# ---------------------------------------------------------------------------
# nordigen connect  — full onboarding flow
# ---------------------------------------------------------------------------

@nordigen.command()
@click.option("--secret-id", envvar="NORDIGEN_SECRET_ID", required=True,
              help="Your GoCardless secret_id.")
@click.option("--secret-key", envvar="NORDIGEN_SECRET_KEY", required=True,
              help="Your GoCardless secret_key.")
@click.option("--refresh-token", envvar="NORDIGEN_REFRESH_TOKEN", required=True,
              help="Your GoCardless refresh token (from 'nordigen auth').")
@click.option("--country", default="GB", show_default=True,
              help="ISO 3166-1 alpha-2 country code to search banks in.")
@click.option("--redirect-url", default="https://example.com/callback", show_default=True,
              help="Redirect URL after bank consent (any valid URL works).")
@click.option("--history-days", default=90, show_default=True,
              help="Days of transaction history to request (max depends on your bank).")
@click.option("--config-file", default="config.yaml", show_default=True,
              type=click.Path(dir_okay=False, path_type=Path),
              help="Path to your config.yaml.")
@click.option("--source-name", default=None,
              help="Name for the source entry in config.yaml (default: auto-generated).")
def connect(
    secret_id: str,
    secret_key: str,
    refresh_token: str,
    country: str,
    redirect_url: str,
    history_days: int,
    config_file: Path,
    source_name: Optional[str],
):
    """
    Connect a bank account and add it to your config.yaml.

    This walks you through the full GoCardless consent flow:
    picking your bank, opening a browser link, and waiting for you to
    complete authentication. Each connected account is saved as a
    separate source entry in config.yaml.
    """
    # If config_file is default and does not exist in CWD, use project root
    if config_file == Path("config.yaml") and not config_file.exists():
        project_root = get_project_root()
        config_file = project_root / "config.yaml"

    # Step 1: get access token
    click.echo("\nStep 1/5 — Authenticating with GoCardless…")
    try:
        token_resp = asyncio.run(refresh_access_token(refresh_token))
        access_token = token_resp.access
    except Exception as exc:
        raise click.ClickException(f"Authentication failed: {exc}") from exc
    click.secho("  ✓ Authenticated", fg="green")

    # Step 2: pick institution
    click.echo(f"\nStep 2/5 — Loading banks for country '{country.upper()}'…")
    try:
        institutions = asyncio.run(list_institutions(access_token, country))
    except Exception as exc:
        raise click.ClickException(f"Failed to load institutions: {exc}") from exc

    if not institutions:
        raise click.ClickException(f"No institutions found for country '{country}'.")

    institution = _pick_institution(institutions)
    click.secho(f"  ✓ Selected: {institution.name} ({institution.id})", fg="green")

    # Step 3: create requisition
    click.echo("\nStep 3/5 — Creating bank consent request…")
    reference = str(uuid.uuid4())[:8]
    try:
        requisition = asyncio.run(
            create_requisition(
                access_token=access_token,
                institution_id=institution.id,
                redirect_url=redirect_url,
                reference=reference,
                max_historical_days=history_days,
            )
        )
    except Exception as exc:
        raise click.ClickException(f"Failed to create requisition: {exc}") from exc

    # Step 4: user consent
    click.echo(f"\nStep 4/5 — Open this link in your browser to grant consent:\n")
    click.secho(f"  {requisition.link}", fg="cyan", bold=True)
    click.echo()

    try:
        webbrowser.open(requisition.link or "")
    except Exception:
        pass

    click.echo("  Complete the bank authentication, then come back here.")
    click.pause("  Press Enter when you have finished…")

    # Step 5: resolve accounts
    click.echo("\nStep 5/5 — Resolving linked accounts…")
    try:
        resolved = asyncio.run(get_requisition(access_token, requisition.id))
    except Exception as exc:
        raise click.ClickException(f"Failed to resolve requisition: {exc}") from exc

    if resolved.status != "LN":
        raise click.ClickException(
            f"Requisition status is '{resolved.status}' (expected 'LN' = Linked). "
            "Make sure you completed the bank authentication."
        )

    account_ids = resolved.accounts
    if not account_ids:
        raise click.ClickException("No accounts were linked. Please try again.")

    selected_ids = _pick_accounts(account_ids)

    # Fetch labels
    click.echo()
    label_input = click.prompt(
        "  Optional: enter a label for this account (e.g. 'Checking', 'Savings')",
        default="",
    ).strip() or None

    # Write to config
    for account_id in selected_ids:
        name = source_name or f"nordigen_{account_id[:8]}"
        _update_config(config_file, name, account_id, label_input)
        click.secho(f"\n  ✓ Added source '{name}' (account: {account_id})", fg="green")

    click.echo(
        f"\nDone! Restart the pipeline to start polling your new account(s).\n"
        f"Config written to: {config_file}\n"
    )


# ---------------------------------------------------------------------------
# Helpers (also used by tests)
# ---------------------------------------------------------------------------

def _pick_institution(institutions: List[Institution]) -> Institution:
    """Interactively search and select an institution from the list."""
    while True:
        query = click.prompt("  Search for your bank (type part of the name)").strip().lower()
        matches = [i for i in institutions if query in i.name.lower() or query in i.id.lower()]

        if not matches:
            click.echo("  No matches found. Try again.")
            continue

        if len(matches) == 1:
            click.echo(f"  Found: {matches[0].name}")
            return matches[0]

        click.echo(f"  Found {len(matches)} matches:")
        for idx, inst in enumerate(matches, 1):
            click.echo(f"    {idx}. {inst.name} ({inst.id})")

        raw = click.prompt("  Enter number to select").strip()
        try:
            choice = int(raw)
            if 1 <= choice <= len(matches):
                return matches[choice - 1]
        except ValueError:
            pass
        click.echo("  Invalid selection. Try again.")


def _pick_accounts(account_ids: List[str]) -> List[str]:
    """
    If there is only one account, return it immediately.
    Otherwise, let the user pick which accounts to add.
    """
    if len(account_ids) == 1:
        click.echo(f"  Found 1 account: {account_ids[0]}")
        return account_ids

    click.echo(f"  Found {len(account_ids)} linked accounts:")
    for idx, acc_id in enumerate(account_ids, 1):
        click.echo(f"    {idx}. {acc_id}")

    raw = click.prompt(
        "  Enter numbers to add (comma-separated), or press Enter for all",
        default="",
    ).strip()

    if not raw:
        return account_ids

    selected = []
    for part in raw.split(","):
        part = part.strip()
        try:
            idx = int(part)
            if 1 <= idx <= len(account_ids):
                selected.append(account_ids[idx - 1])
        except ValueError:
            pass

    return selected if selected else account_ids


def _update_config(config_file: Path, source_name: str, account_id: str, label: Optional[str]) -> None:
    """
    Write or update a single nordigen source entry in config.yaml.

    Each account gets its own top-level source entry. Existing entries are
    preserved. If the account_id already exists under this source name, it
    is skipped (no duplicates).
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True

    if config_file.exists():
        with config_file.open("r", encoding="utf-8") as fp:
            data = yaml.load(fp) or {}
    else:
        data = {}

    sources = data.setdefault("sources", {})
    existing = sources.get(source_name, {})

    # If this source already has this account_id, skip
    if existing.get("account_id") == account_id:
        click.echo(f"  Account '{account_id}' already present in source '{source_name}', skipping.")
        return

    entry: dict = {
        "type": "nordigen",
        "account_id": account_id,
    }
    if label:
        entry["label"] = label

    sources[source_name] = entry
    with config_file.open("w", encoding="utf-8") as fp:
        yaml.dump(data, fp)
