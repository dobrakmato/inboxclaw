import click
import uvicorn
import logging
import os
from typing import Optional
from src.cli import cli
from src.app import app
from src.config import load_config
from src.utils.paths import get_project_root

logger = logging.getLogger("inboxclaw")

@cli.command()
@click.option("--config", "config_path", default=None, help="Path to the configuration file.")
def listen(config_path: Optional[str]):
    """Start the Inboxclaw server."""
    if config_path is None:
        # Check if config.yaml exists in current directory, otherwise use project root
        if os.path.exists("config.yaml"):
            config_path = "config.yaml"
        else:
            project_root = get_project_root()
            config_path = str(project_root / "config.yaml")

    # Pass the config path to the app state so lifespan can use it
    app.state.config_path = config_path
    
    conf = load_config(config_path)
    logger.info(f"Starting server on {conf.server.host}:{conf.server.port}")
    uvicorn.run(app, host=conf.server.host, port=conf.server.port)
