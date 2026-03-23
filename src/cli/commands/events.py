import click
import os
import sys
from typing import Optional
from sqlalchemy import select, desc

from src.cli import cli
from src.config import load_config
from src.database import init_db, Event, PendingEvent, Source
from src.utils.paths import get_project_root

def get_db_session(config_path: Optional[str]):
    project_root = get_project_root()
    
    if config_path is None:
        if os.path.exists("config.yaml"):
            config_path = "config.yaml"
        else:
            config_path = str(project_root / "config.yaml")
    
    try:
        config = load_config(config_path)
    except Exception as e:
        click.secho(f"Error loading config from {config_path}: {e}", fg="red")
        sys.exit(1)

    db_path = config.database.db_path
    if not os.path.isabs(db_path):
        config_dir = os.path.dirname(os.path.abspath(config_path))
        db_path = os.path.join(config_dir, db_path)
            
    session_maker = init_db(db_path)
    return session_maker()

@cli.command()
@click.option("-n", default=10, help="Number of latest published events to display (default: 10).")
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
def events(n: int, config_path: Optional[str]):
    """Display latest N published events."""
    session = get_db_session(config_path)
    try:
        stmt = select(Event).order_by(desc(Event.created_at)).limit(n)
        results = session.execute(stmt).scalars().all()
        
        if not results:
            click.echo("No published events found.")
            return

        click.secho(f"=== Latest {len(results)} Published Events ===", bold=True)
        for event in results:
            # We need to load the source name, it might not be eager loaded
            source_name = event.source.name if event.source else f"ID:{event.source_id}"
            click.echo(f"[{event.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {event.event_id} | {source_name} | {event.event_type} | {event.entity_id or 'N/A'}")
            # Optional: show data if n is small? User didn't specify. I'll keep it concise for now.
    finally:
        session.close()

@cli.command("pending-events")
@click.option("-n", default=10, help="Number of latest pending events to display (default: 10).")
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
def pending_events(n: int, config_path: Optional[str]):
    """Display latest N pending events."""
    session = get_db_session(config_path)
    try:
        stmt = select(PendingEvent).order_by(desc(PendingEvent.last_seen_at)).limit(n)
        results = session.execute(stmt).scalars().all()
        
        if not results:
            click.echo("No pending events found.")
            return

        click.secho(f"=== Latest {len(results)} Pending Events ===", bold=True)
        for event in results:
            # Manually fetch source name for clarity
            source = session.get(Source, event.source_id)
            source_name = source.name if source else f"ID:{event.source_id}"
            click.echo(f"[{event.last_seen_at.strftime('%Y-%m-%d %H:%M:%S')}] {source_name} | {event.event_type} | {event.entity_id or 'N/A'} (Count: {event.count}, Flush at: {event.flush_at.strftime('%H:%M:%S')})")
    finally:
        session.close()
