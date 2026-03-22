import click
import httpx
import os
import sys
import logging
from typing import Optional, List
from src.cli import cli
from src.config import load_config, HttpPullSinkConfig
from src.utils.paths import get_project_root

logger = logging.getLogger("inboxclaw")

@cli.command()
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
@click.option("--name", "sink_name", default=None, help="Name of the HTTP Pull sink to use (if multiple are configured).")
@click.option("--event-type", default=None, help="Filter by event type (supports * and .*)")
@click.option("--batch-size", default=None, type=int, help="Limit the number of events to extract (must be >= 1)")
def pull(config_path: Optional[str], sink_name: Optional[str], event_type: Optional[str], batch_size: Optional[int]):
    """Run a pull request against a locally configured HTTP Pull sink."""
    project_root = get_project_root()
    
    if config_path is None:
        if os.path.exists("config.yaml"):
            config_path = "config.yaml"
        else:
            config_path = str(project_root / "config.yaml")
    
    try:
        config = load_config(config_path)
    except Exception as e:
        click.secho(f"Error loading config from {config_path}: {e}", fg="red", err=True)
        sys.exit(1)

    # Find HTTP Pull sinks
    http_pull_sinks = {
        name: cfg for name, cfg in config.sink.items() 
        if isinstance(cfg, HttpPullSinkConfig)
    }

    if not http_pull_sinks:
        click.secho("No HTTP Pull sinks configured in this instance.", fg="red", err=True)
        sys.exit(1)

    if sink_name:
        if sink_name not in http_pull_sinks:
            click.secho(f"HTTP Pull sink '{sink_name}' not found in configuration.", fg="red", err=True)
            sys.exit(1)
        selected_sink_name = sink_name
        selected_sink_cfg = http_pull_sinks[sink_name]
    else:
        if len(http_pull_sinks) > 1:
            click.secho("Multiple HTTP Pull sinks found. Please specify one with --name:", fg="yellow", err=True)
            for name in http_pull_sinks:
                click.echo(f" - {name}", err=True)
            sys.exit(1)
        
        selected_sink_name, selected_sink_cfg = next(iter(http_pull_sinks.items()))

    # Build the URL
    host = config.server.host
    if host == "0.0.0.0":
        host = "127.0.0.1"
    
    base_url = f"http://{host}:{config.server.port}"
    extract_suffix = selected_sink_cfg.path.get("extract", "extract").lstrip("/")
    extract_url = f"{base_url}/{selected_sink_name}/{extract_suffix}"
    
    # 1. Extract
    params = {}
    if event_type:
        params["event_type"] = event_type
    if batch_size:
        params["batch_size"] = batch_size
        
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(extract_url, params=params)
            
            if response.status_code != 200:
                click.secho(f"Extraction failed with status {response.status_code}: {response.text}", fg="red", err=True)
                sys.exit(1)
                
            data = response.json()
            events = data.get("events", [])
            
            if not events:
                return

            # Output the JSON directly to stdout
            import json
            click.echo(json.dumps(data))
                
    except httpx.RequestError as e:
        click.secho(f"Error connecting to server at {base_url}: {e}", fg="red", err=True)
        click.echo("Is the Inboxclaw server running? Use 'inboxclaw listen' to start it.", err=True)
        sys.exit(1)

@cli.command("pull-mark-processed")
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
@click.option("--name", "sink_name", default=None, help="Name of the HTTP Pull sink to use.")
@click.option("--batch-id", required=True, help="Batch ID to mark as processed.")
def pull_mark_processed(config_path: Optional[str], sink_name: Optional[str], batch_id: str):
    """Mark a batch as processed in a locally configured HTTP Pull sink."""
    project_root = get_project_root()
    
    if config_path is None:
        if os.path.exists("config.yaml"):
            config_path = "config.yaml"
        else:
            config_path = str(project_root / "config.yaml")
    
    try:
        config = load_config(config_path)
    except Exception as e:
        click.secho(f"Error loading config from {config_path}: {e}", fg="red", err=True)
        sys.exit(1)

    # Find HTTP Pull sinks
    http_pull_sinks = {
        name: cfg for name, cfg in config.sink.items() 
        if isinstance(cfg, HttpPullSinkConfig)
    }

    if not http_pull_sinks:
        click.secho("No HTTP Pull sinks configured in this instance.", fg="red", err=True)
        sys.exit(1)

    if sink_name:
        if sink_name not in http_pull_sinks:
            click.secho(f"HTTP Pull sink '{sink_name}' not found in configuration.", fg="red", err=True)
            sys.exit(1)
        selected_sink_name = sink_name
        selected_sink_cfg = http_pull_sinks[sink_name]
    else:
        if len(http_pull_sinks) > 1:
            click.secho("Multiple HTTP Pull sinks found. Please specify one with --name:", fg="yellow", err=True)
            for name in http_pull_sinks:
                click.echo(f" - {name}", err=True)
            sys.exit(1)
        
        selected_sink_name, selected_sink_cfg = next(iter(http_pull_sinks.items()))

    # Build the URL
    host = config.server.host
    if host == "0.0.0.0":
        host = "127.0.0.1"
    
    base_url = f"http://{host}:{config.server.port}"
    mark_processed_suffix = selected_sink_cfg.path.get("mark_processed", "mark-processed").lstrip("/")
    mark_url = f"{base_url}/{selected_sink_name}/{mark_processed_suffix}"
    
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(mark_url, params={"batch_id": batch_id})
            
            if response.status_code != 200:
                click.secho(f"Failed to mark batch {batch_id} as processed: {response.text}", fg="red", err=True)
                sys.exit(1)
            
            click.secho(f"Successfully marked batch {batch_id} as processed.", fg="green")
                
    except httpx.RequestError as e:
        click.secho(f"Error connecting to server at {base_url}: {e}", fg="red", err=True)
        sys.exit(1)
