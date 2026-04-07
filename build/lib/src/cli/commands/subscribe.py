import click
import asyncio
import httpx
import json
import os
import sys
from typing import Optional
from src.cli import cli
from src.config import load_config
from src.utils.paths import get_project_root

@cli.command()
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
@click.option("--sink", "sink_name", default=None, help="Specific SSE sink name to use.")
def subscribe(config_path: Optional[str], sink_name: Optional[str]):
    """Subscribe to SSE endpoint and dump raw JSON payloads to stdout."""
    if config_path is None:
        if os.path.exists("config.yaml"):
            config_path = "config.yaml"
        else:
            project_root = get_project_root()
            config_path = str(project_root / "config.yaml")

    try:
        conf = load_config(config_path)
    except Exception as e:
        click.echo(f"Error loading configuration: {e}", err=True)
        sys.exit(1)

    # Find SSE sinks
    sse_sinks = {name: cfg for name, cfg in conf.sink.items() if cfg.type == "sse"}

    if not sse_sinks:
        click.echo("No SSE sink configured in the configuration file.", err=True)
        sys.exit(1)

    if sink_name:
        if sink_name not in sse_sinks:
            click.echo(f"SSE sink '{sink_name}' not found in configuration.", err=True)
            click.echo(f"Available SSE sinks: {', '.join(sse_sinks.keys())}", err=True)
            sys.exit(1)
        selected_sink_name = sink_name
    else:
        # Use the first one if not specified
        selected_sink_name = list(sse_sinks.keys())[0]
        if len(sse_sinks) > 1:
            click.echo(f"Multiple SSE sinks found. Using '{selected_sink_name}'.", err=True)

    sink_config = sse_sinks[selected_sink_name]
    
    # Construct URL
    # SSESink.path logic: f"/{name}/{config.path}"
    # Default path in config is ""
    path = f"/{selected_sink_name}"
    if hasattr(sink_config, 'path') and sink_config.path:
        path = f"{path.rstrip('/')}/{sink_config.path.lstrip('/')}"
    else:
        path = f"{path}/"

    base_url = f"http://{conf.server.host}:{conf.server.port}"
    # Handle 0.0.0.0 for connecting - replace with localhost
    if conf.server.host == "0.0.0.0":
        base_url = f"http://127.0.0.1:{conf.server.port}"
    
    url = f"{base_url}{path}"
    
    click.echo(f"Connecting to {url}...", err=True)

    try:
        asyncio.run(stream_sse(url))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

async def stream_sse(url: str):
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    click.echo(f"Failed to connect: {response.status_code} {response.reason_phrase}", err=True)
                    return

                # Simple SSE parser
                # SSE format:
                # event: name
                # data: payload
                # id: value
                # (empty line separates events)
                
                current_event = {}
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        # End of event
                        if "data" in current_event:
                            if current_event.get("event") == "message":
                                # Print only data for messages
                                print(current_event["data"])
                                sys.stdout.flush()
                            elif current_event.get("event") == "info":
                                click.echo(f"Info: {current_event['data']}", err=True)
                        current_event = {}
                        continue

                    if ":" in line:
                        field, value = line.split(":", 1)
                        field = field.strip()
                        value = value.strip()
                        current_event[field] = value
        except httpx.ReadError as e:
            click.echo(f"Connection lost: {e}", err=True)
        except Exception as e:
            click.echo(f"Stream error: {e}", err=True)
