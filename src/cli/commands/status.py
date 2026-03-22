import click
import subprocess
import os
import sys
import logging
import httpx
from datetime import datetime
from typing import Optional
from sqlalchemy import select, func

from src.cli import cli
from src.config import load_config
from src.database import init_db, Source, Event, PendingEvent, Sink
from src.utils.paths import get_project_root

logger = logging.getLogger("inboxclaw")

@cli.command()
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
@click.option("--service-name", default="inboxclaw", help="Name of the systemd service.")
def status(config_path: Optional[str], service_name: str):
    """Check the status of the Inboxclaw system."""
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

    click.secho("=== Inboxclaw Status ===", bold=True)
    
    # 1. Systemd Service Status
    click.echo("\n[Systemd Service]")
    if os.name != 'posix':
        click.echo("Systemd is only supported on Linux/POSIX systems.")
    else:
        try:
            # Check user service first
            res = subprocess.run(["systemctl", "--user", "is-active", service_name], capture_output=True, text=True)
            active_status = res.stdout.strip()
            if active_status != "active":
                # Check system service
                res = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
                active_status = res.stdout.strip()
            
            if active_status == "active":
                click.secho(f"Service '{service_name}': {active_status}", fg="green")
            else:
                click.secho(f"Service '{service_name}': {active_status}", fg="red")
        except Exception as e:
            click.echo(f"Could not check systemd status: {e}")

    # 2. Last 5 lines from logs
    click.echo("\n[Last 5 Log Lines]")
    if os.name == 'posix':
        try:
            # Try journalctl first
            res = subprocess.run(["journalctl", "--user", "-u", service_name, "-n", "5", "--no-pager"], capture_output=True, text=True)
            if not res.stdout.strip():
                 res = subprocess.run(["journalctl", "-u", service_name, "-n", "5", "--no-pager"], capture_output=True, text=True)
            
            if res.stdout.strip():
                click.echo(res.stdout.strip())
            else:
                click.echo("No logs found in journalctl.")
        except Exception as e:
            click.echo(f"Could not retrieve logs from journalctl: {e}")
    else:
        click.echo("Log retrieval via journalctl is only supported on Linux.")

    # 3. Healthcheck
    click.echo("\n[Healthcheck]")
    url = f"http://{config.server.host}:{config.server.port}/healthcheck"
    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get(url)
            if response.status_code == 200:
                click.secho(f"Endpoint {url}: OK", fg="green")
            else:
                click.secho(f"Endpoint {url}: Error {response.status_code}", fg="red")
    except Exception as e:
        click.secho(f"Endpoint {url}: Unreachable ({e})", fg="red")

    # 4. Version Update
    click.echo("\n[Version Info]")
    try:
        # Get current version from pyproject.toml
        pyproject_path = project_root / "pyproject.toml"
        current_version = "Unknown"
        if pyproject_path.exists():
            with open(pyproject_path, "rb") as f:
                if sys.version_info >= (3, 11):
                    import tomllib
                    pyproject_data = tomllib.load(f)
                    current_version = pyproject_data.get("project", {}).get("version", "Unknown")
                else:
                    # Fallback for older python or just simple string search
                    for line in f.read().decode().splitlines():
                        if line.strip().startswith("version ="):
                            current_version = line.split("=")[1].strip().strip('"').strip("'")
                            break
        
        click.echo(f"Current version: {current_version}")
        
        # Check latest version from git
        if (project_root / ".git").exists():
            subprocess.run(["git", "fetch"], capture_output=True, check=True)
            local_branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True, cwd=project_root).strip()
            try:
                upstream_branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", f"{local_branch}@{{u}}"], text=True, stderr=subprocess.PIPE, cwd=project_root).strip()
                remote_commit = subprocess.check_output(["git", "rev-parse", upstream_branch], text=True, cwd=project_root).strip()
                local_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=project_root).strip()
                
                if local_commit == remote_commit:
                    click.secho("Git Status: Up to date", fg="green")
                else:
                    click.secho(f"Git Status: Update available (Remote: {remote_commit[:7]})", fg="yellow")
            except Exception:
                click.echo("Git Status: No upstream branch configured.")
        else:
            click.echo("Git Status: Not a git repository.")
    except Exception as e:
        click.echo(f"Could not check version: {e}")

    # 5. Database Stats
    click.echo("\n[Database Stats]")
    try:
        session_maker = init_db(config.database.db_path)
        with session_maker() as session:
            num_sources = session.query(func.count(Source.id)).scalar()
            num_events = session.query(func.count(Event.id)).scalar()
            num_pending = session.query(func.count(PendingEvent.id)).scalar()
            num_sinks = session.query(func.count(Sink.id)).scalar()
            
            click.echo(f"Sources configured: {num_sources}")
            click.echo(f"Sinks configured: {num_sinks}")
            click.echo(f"Total events in DB: {num_events}")
            click.echo(f"Pending (coalescing) events: {num_pending}")
            
            if num_sources > 0:
                click.echo("\n[Sources Detail]")
                sources = session.execute(select(Source)).scalars().all()
                for s in sources:
                    click.echo(f" - {s.name} ({s.type}): cursor={s.cursor or 'None'}")
                    
    except Exception as e:
        click.secho(f"Error reading database: {e}", fg="red")
