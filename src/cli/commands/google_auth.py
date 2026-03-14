import click
import json
import os
import secrets
from pathlib import Path
from typing import Optional, List, Dict
from google_auth_oauthlib.flow import InstalledAppFlow
from src.cli import cli

# Human-friendly aliases for Google Scopes
SCOPE_MAPPING: Dict[str, str] = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "drive": "https://www.googleapis.com/auth/drive.metadata.readonly",
    "calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "docs": "https://www.googleapis.com/auth/drive.metadata.readonly", # Docs info often comes from Drive metadata
    "contacts": "https://www.googleapis.com/auth/contacts.readonly",
    # Add more as needed
}

def build_client_config(client_id: str, client_secret: str) -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost", "http://127.0.0.1"],
        }
    }

@cli.group()
def google():
    """Google API related commands."""
    pass

@google.command()
@click.option("--credentials-file", type=click.Path(exists=True), help="Path to Google OAuth credentials JSON file.")
@click.option("--client-id", help="Google OAuth Client ID.")
@click.option("--client-secret", help="Google OAuth Client Secret.")
@click.option("--scopes", "scopes_input", required=True, help=f"Comma-delimited list of scope aliases (e.g., {', '.join(SCOPE_MAPPING.keys())}).")
@click.option("--token", "token_path", required=True, type=click.Path(), help="Path where to save the token.")
def auth(credentials_file: Optional[str], client_id: Optional[str], client_secret: Optional[str], scopes_input: str, token_path: str):
    """Perform Google OAuth flow to obtain a token."""
    aliases = [s.strip().lower() for s in scopes_input.split(",")]
    scopes = []
    
    for alias in aliases:
        if alias in SCOPE_MAPPING:
            scopes.append(SCOPE_MAPPING[alias])
        else:
            # Check if user passed a raw URL (still allow for flexibility, but prioritize aliases)
            if alias.startswith("https://"):
                 scopes.append(alias)
            else:
                raise click.UsageError(f"Unknown scope alias: '{alias}'. Available: {', '.join(SCOPE_MAPPING.keys())}")

    if credentials_file:
        flow = InstalledAppFlow.from_client_secrets_file(credentials_file, scopes)
    elif client_id and client_secret:
        client_config = build_client_config(client_id, client_secret)
        flow = InstalledAppFlow.from_client_config(client_config, scopes)
    else:
        # Check environment variables
        env_client_id = os.environ.get("GOOGLE_CLIENT_ID")
        env_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        if env_client_id and env_client_secret:
            client_config = build_client_config(env_client_id, env_client_secret)
            flow = InstalledAppFlow.from_client_config(client_config, scopes)
        else:
            raise click.UsageError("Provide either --credentials-file or both --client-id and --client-secret (or GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET envs).")

    # Manual flow as requested (copy & paste link)
    redirect_uri = "http://127.0.0.1:8765/"
    flow.redirect_uri = redirect_uri
    
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=secrets.token_urlsafe(32),
    )
    
    click.echo("\nStep 1: Open this URL in your browser and finish Google sign-in:")
    click.echo(auth_url)
    
    click.echo("\nStep 2: After approval, the browser will redirect to a URL that might fail to load.")
    click.echo(f"It should look like {redirect_uri}?code=...&scope=...")
    authorization_response = click.prompt("\nPaste the full redirected URL")
    
    flow.fetch_token(authorization_response=authorization_response)
    
    token_file = Path(token_path)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    with open(token_file, "w") as f:
        f.write(flow.credentials.to_json())
    
    click.echo(f"\nToken successfully saved to {token_path}")
