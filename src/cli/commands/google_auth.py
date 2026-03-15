import os
from pathlib import Path
from typing import Dict
from urllib.parse import parse_qs, urlsplit, urlunsplit

import click
from google_auth_oauthlib.flow import InstalledAppFlow

from src.cli import cli

# Human-friendly aliases for Google scopes.
SCOPE_MAPPING: Dict[str, str] = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "drive": "https://www.googleapis.com/auth/drive.readonly",
    "drive_metadata": "https://www.googleapis.com/auth/drive.metadata.readonly",
    "calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "docs": "https://www.googleapis.com/auth/drive.readonly",
    "contacts": "https://www.googleapis.com/auth/contacts.readonly",
    "all": "gmail,drive,calendar,contacts",
}

REDIRECT_URI = "http://127.0.0.1:8765/"


def resolve_scopes(scopes_input: str) -> list[str]:
    scopes: list[str] = []

    to_process = [s.strip() for s in scopes_input.split(",") if s.strip()]
    seen_aliases = set()

    while to_process:
        raw = to_process.pop(0)
        alias = raw.lower()

        if alias in seen_aliases:
            continue
        seen_aliases.add(alias)

        if alias in SCOPE_MAPPING:
            mapped_value = SCOPE_MAPPING[alias]
            if "," in mapped_value:
                # Recursive alias like "all"
                to_process.extend([s.strip() for s in mapped_value.split(",") if s.strip()])
            else:
                scopes.append(mapped_value)
            continue

        if raw.startswith("https://"):
            scopes.append(raw)
            continue

        raise click.UsageError(
            f"Unknown scope alias: '{raw}'. Available: {', '.join(SCOPE_MAPPING.keys())}"
        )

    if not scopes:
        raise click.UsageError("At least one scope must be provided.")

    return scopes


def _normalized_path(path: str) -> str:
    return path or "/"


def normalize_authorization_response(
        pasted_url: str,
        expected_redirect_uri: str,
) -> str:
    raw = pasted_url.strip().strip('"').strip("'")
    parsed = urlsplit(raw)
    expected = urlsplit(expected_redirect_uri)

    if not parsed.scheme or not parsed.netloc:
        raise click.ClickException(
            "Paste the full redirected URL, not just the code."
        )

    if (
            parsed.scheme != expected.scheme
            or (parsed.hostname or "").lower() != (expected.hostname or "").lower()
            or parsed.port != expected.port
            or _normalized_path(parsed.path) != _normalized_path(expected.path)
    ):
        raise click.ClickException(
            "The pasted URL does not match the expected redirect URL.\n"
            f"Expected base: {expected_redirect_uri}\n"
            f"Got: {raw}"
        )

    params = parse_qs(parsed.query)

    if "error" in params:
        error = params["error"][0]
        description = params.get("error_description", [""])[0]
        details = f": {description}" if description else ""
        raise click.ClickException(f"Google returned an OAuth error '{error}'{details}")

    if "code" not in params:
        raise click.ClickException(
            "The pasted URL does not contain an authorization code."
        )

    # Drop any fragment just in case and return a clean URL.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


@cli.group()
def google():
    """Google API related commands."""
    pass


@google.command()
@click.option(
    "--credentials-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a Google OAuth Desktop App credentials JSON file.",
)
@click.option(
    "--scopes",
    "scopes_input",
    required=True,
    help=f"Comma-delimited list of scope aliases (e.g., {', '.join(SCOPE_MAPPING.keys())}).",
)
@click.option(
    "--token",
    "token_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path where to save the token JSON.",
)
def auth(credentials_file: Path, scopes_input: str, token_path: Path):
    """Perform Google OAuth flow to obtain and save a token."""
    scopes = resolve_scopes(scopes_input)

    # Required because the redirect URI is http://127.0.0.1:8765/.
    # In this flow there is no local listener; the user pastes the final URL back.
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_file),
            scopes=scopes,
            autogenerate_code_verifier=True,
        )
    except Exception as err:
        raise click.ClickException(f"Failed to load credentials file: {err}") from err

    flow.redirect_uri = REDIRECT_URI

    try:
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )
    except Exception as err:
        raise click.ClickException(
            f"Failed to generate authorization URL: {err}"
        ) from err

    click.echo("\nStep 1: Open this URL in your browser and complete Google sign-in:\n")
    click.echo(auth_url)

    click.echo(
        "\nStep 2: After approval, Google will redirect the browser to a URL like:\n"
        f"  {REDIRECT_URI}?code=...&scope=...\n"
        "The page will likely fail to load. That is expected."
    )

    pasted_url = click.prompt("\nPaste the full redirected URL")
    authorization_response = normalize_authorization_response(
        pasted_url,
        REDIRECT_URI,
    )

    try:
        token_response = flow.fetch_token(
            authorization_response=authorization_response,
        )
    except Exception as err:
        raise click.ClickException(f"Failed to fetch token: {err}") from err

    credentials = flow.credentials
    if credentials is None:
        raise click.ClickException("No credentials were produced by the OAuth flow.")

    try:
        token_file = token_path.expanduser().resolve()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
    except Exception as err:
        raise click.ClickException(
            f"Failed to save credentials: {err}"
        ) from err

    click.echo(f"\nToken successfully saved to: {token_file}")

    granted_scopes = set(credentials.scopes or [])
    requested_scopes = set(scopes)
    if granted_scopes and granted_scopes != requested_scopes:
        click.secho(
            "\n[NOTE] Granted scopes differ from requested scopes.",
            fg="yellow",
        )
        click.echo("Granted scopes:")
        for scope in sorted(granted_scopes):
            click.echo(f"  - {scope}")

    token_scope = token_response.get("scope", "")
    if token_scope:
        click.echo("\nToken scope returned by Google:")
        click.echo(token_scope)
