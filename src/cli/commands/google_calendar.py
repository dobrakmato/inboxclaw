import click
from pathlib import Path
from googleapiclient.discovery import build
from src.cli.commands.google_auth import google
from src.utils.google_auth import get_google_credentials

@google.command()
@click.option(
    "--token-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the Google token file.",
)
def list_calendars(token_file: Path):
    """List available Google Calendars with their names and IDs."""
    try:
        creds = get_google_credentials(str(token_file), "CLI")
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        
        click.echo("Fetching calendar list...")
        
        calendar_list = service.calendarList().list().execute()
        items = calendar_list.get('items', [])
        
        if not items:
            click.echo("No calendars found.")
            return
            
        click.echo(f"{'NAME':<30} {'ID':<50}")
        click.echo("-" * 80)
        
        for item in items:
            summary = item.get('summary', 'Unknown Name')
            calendar_id = item.get('id', 'Unknown ID')
            click.echo(f"{summary:<30} {calendar_id:<50}")
            
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
