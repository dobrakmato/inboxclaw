import click
import uvicorn
import logging
from typing import Optional
from src.cli import cli
from src.app import app
from src.config import load_config

logger = logging.getLogger("inboxclaw")

@cli.command()
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
def listen(config_path: Optional[str]):
    """Start the Inboxclaw server."""
    # Pass the config path to the app state so lifespan can use it
    app.state.config_path = config_path
    
    conf = load_config(config_path)
    logger.info(f"Starting server on {conf.server.host}:{conf.server.port}")
    uvicorn.run(app, host=conf.server.host, port=conf.server.port)
