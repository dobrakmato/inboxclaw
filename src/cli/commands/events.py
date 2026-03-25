import click
import json
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
@click.option("-j", "as_json", is_flag=True, default=False, help="Output events as JSON objects.")
@click.option("--source", help="Filter events by source name.")
@click.option("--event-type", help="Filter events by event type.")
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
def events(n: int, as_json: bool, source: Optional[str], event_type: Optional[str], config_path: Optional[str]):
    """Display latest N published events."""
    session = get_db_session(config_path)
    try:
        stmt = select(Event).order_by(desc(Event.created_at))
        
        if source:
            stmt = stmt.join(Source).where(Source.name == source)
        if event_type:
            stmt = stmt.where(Event.event_type == event_type)
            
        stmt = stmt.limit(n)
        results = session.execute(stmt).scalars().all()
        
        if not results:
            click.echo("No published events found.")
            return

        if as_json:
            items = []
            for event in results:
                source_name = event.source.name if event.source else f"ID:{event.source_id}"
                items.append({
                    "id": event.id,
                    "event_id": event.event_id,
                    "source": source_name,
                    "event_type": event.event_type,
                    "entity_id": event.entity_id,
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                    "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
                    "data": event.data,
                    "meta": event.meta,
                })
            click.echo(json.dumps(items, indent=2))
        else:
            click.secho(f"=== Latest {len(results)} Published Events ===", bold=True)
            for event in results:
                source_name = event.source.name if event.source else f"ID:{event.source_id}"
                click.echo(f"[{event.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {event.event_id} | {source_name} | {event.event_type} | {event.entity_id or 'N/A'}")
    finally:
        session.close()

@cli.command("pending-events")
@click.option("-n", default=10, help="Number of latest pending events to display (default: 10).")
@click.option("-j", "as_json", is_flag=True, default=False, help="Output pending events as JSON objects.")
@click.option("--source", help="Filter pending events by source name.")
@click.option("--event-type", help="Filter pending events by event type.")
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
def pending_events(n: int, as_json: bool, source: Optional[str], event_type: Optional[str], config_path: Optional[str]):
    """Display latest N pending events."""
    session = get_db_session(config_path)
    try:
        stmt = select(PendingEvent).order_by(desc(PendingEvent.last_seen_at))
        
        if source:
            stmt = stmt.join(Source).where(Source.name == source)
        if event_type:
            stmt = stmt.where(PendingEvent.event_type == event_type)
            
        stmt = stmt.limit(n)
        results = session.execute(stmt).scalars().all()
        
        if not results:
            click.echo("No pending events found.")
            return

        if as_json:
            items = []
            for event in results:
                source = session.get(Source, event.source_id)
                source_name = source.name if source else f"ID:{event.source_id}"
                items.append({
                    "id": event.id,
                    "source": source_name,
                    "event_type": event.event_type,
                    "entity_id": event.entity_id,
                    "data": event.data,
                    "meta": event.meta,
                    "count": event.count,
                    "first_seen_at": event.first_seen_at.isoformat() if event.first_seen_at else None,
                    "last_seen_at": event.last_seen_at.isoformat() if event.last_seen_at else None,
                    "flush_at": event.flush_at.isoformat() if event.flush_at else None,
                    "strategy": event.strategy,
                    "window_seconds": event.window_seconds,
                })
            click.echo(json.dumps(items, indent=2))
        else:
            click.secho(f"=== Latest {len(results)} Pending Events ===", bold=True)
            for event in results:
                source = session.get(Source, event.source_id)
                source_name = source.name if source else f"ID:{event.source_id}"
                click.echo(f"[{event.last_seen_at.strftime('%Y-%m-%d %H:%M:%S')}] {source_name} | {event.event_type} | {event.entity_id or 'N/A'} (Count: {event.count}, Flush at: {event.flush_at.strftime('%H:%M:%S')})")
    finally:
        session.close()
