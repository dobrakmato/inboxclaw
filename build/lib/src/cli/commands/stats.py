import click
import os
import sys
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy import select, func, and_

from src.cli import cli
from src.config import load_config
from src.database import init_db, Source, Event
from src.utils.paths import get_project_root

logger = logging.getLogger("inboxclaw")

@cli.command()
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
def stats(config_path: Optional[str]):
    """Compute and show Inboxclaw stats."""
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

    # Resolve db_path
    db_path = config.database.db_path
    if not os.path.isabs(db_path):
        config_dir = os.path.dirname(os.path.abspath(config_path))
        db_path = os.path.join(config_dir, db_path)
        
    try:
        session_maker = init_db(db_path)
        with session_maker() as session:
            now = datetime.now(timezone.utc)
            intervals = {
                "1d": now - timedelta(days=1),
                "7d": now - timedelta(days=7),
                "30d": now - timedelta(days=30),
            }

            click.secho("=== Inboxclaw Stats ===", bold=True)

            # 1. Stats from each source
            click.echo("\n[Events by Source]")
            sources = session.execute(select(Source)).scalars().all()
            source_map = {s.id: s.name for s in sources}
            
            source_stats = {}
            for interval_name, cutoff in intervals.items():
                stmt = select(Event.source_id, func.count(Event.id)).where(Event.created_at >= cutoff).group_by(Event.source_id)
                results = session.execute(stmt).all()
                
                for source_id, count in results:
                    source_name = source_map.get(source_id, f"Unknown({source_id})")
                    if source_name not in source_stats:
                        source_stats[source_name] = {"1d": 0, "7d": 0, "30d": 0}
                    source_stats[source_name][interval_name] = count

            if not source_stats:
                click.echo("No events found in the past 30 days.")
            else:
                # Table header
                click.echo(f"{'Source':<20} {'1d':>5} {'7d':>5} {'30d':>5}")
                click.echo("-" * 40)
                for source_name in sorted(source_stats.keys()):
                    s = source_stats[source_name]
                    click.echo(f"{source_name:<20} {s['1d']:>5} {s['7d']:>5} {s['30d']:>5}")

            # 2. Stats of each type
            click.echo("\n[Events by Type]")
            type_stats = {}
            for interval_name, cutoff in intervals.items():
                stmt = select(Event.event_type, func.count(Event.id)).where(Event.created_at >= cutoff).group_by(Event.event_type)
                results = session.execute(stmt).all()
                
                for event_type, count in results:
                    if event_type not in type_stats:
                        type_stats[event_type] = {"1d": 0, "7d": 0, "30d": 0}
                    type_stats[event_type][interval_name] = count

            if not type_stats:
                click.echo("No events found in the past 30 days.")
            else:
                # Table header
                click.echo(f"{'Type':<20} {'1d':>5} {'7d':>5} {'30d':>5}")
                click.echo("-" * 40)
                for event_type in sorted(type_stats.keys()):
                    t = type_stats[event_type]
                    click.echo(f"{event_type:<20} {t['1d']:>5} {t['7d']:>5} {t['30d']:>5}")

            # 3. Average size of event by event type
            click.echo("\n[Average Event Size (Bytes)]")
            # We iterate over all events in DB for the average size calculation
            # or we can limit it to 30 days. Let's do it for all as requested.
            
            # Using Python to calculate size since JSON length in SQL depends on DB engine
            # and SQLite doesn't have a direct length(JSON_COLUMN) that is reliable for all.
            # But Event.data is a JSON column (SQLAlchemy type).
            
            # Actually we can use func.length(func.cast(Event.data, String)) in SQLite
            # but it might be safer to do it in Python if the DB size is reasonable.
            # For a stats command, it's probably fine.
            
            stmt = select(Event.event_type).distinct()
            all_types = session.execute(stmt).scalars().all()
            if not all_types:
                click.echo("No events found.")
            else:
                click.echo(f"{'Type':<20} {'Avg Size':>10}")
                click.echo("-" * 32)
                for etype in sorted(all_types):
                    # Fetch all data for this type
                    events = session.execute(select(Event.data).where(Event.event_type == etype)).scalars().all()
                    if not events:
                        continue
                    
                    total_size = 0
                    for data in events:
                        if data is None:
                            continue
                        # Standardize to stringified JSON for size estimate
                        size = len(json.dumps(data, separators=(',', ':')))
                        total_size += size
                    
                    avg_size = total_size / len(events)
                    click.echo(f"{etype:<20} {avg_size:>10.1f}")

    except Exception as e:
        click.secho(f"Error reading database: {e}", fg="red")
        sys.exit(1)
