import click
import subprocess
import os
import sys
from typing import Optional

from src.cli import cli
from src.config import load_config
from src.utils.paths import get_project_root

def get_uid() -> int:
    """Get the current user's UID. Returns -1 on non-POSIX systems."""
    if os.name == 'posix':
        return os.getuid()
    return -1

@cli.command()
@click.option("--service-name", default="inboxclaw", help="Name of the systemd service to restart.")
@click.option("--user", "is_user", is_flag=True, default=True, help="Restart as a user service (default).")
@click.option("--system", "is_system", is_flag=True, help="Restart as a system-wide service (requires root).")
@click.option("--config", "config_path", help="Path to the configuration file to validate.")
def restart(service_name: str, is_user: bool, is_system: bool, config_path: Optional[str]):
    """Restart the Inboxclaw systemd service."""
    if os.name != 'posix':
        click.echo("Systemd is only supported on Linux/POSIX systems. Skipping service restart.")
        return

    if config_path is None:
        # Check if config.yaml exists in current directory, otherwise use project root
        if os.path.exists("config.yaml"):
            config_path = "config.yaml"
        else:
            project_root = get_project_root()
            config_path = str(project_root / "config.yaml")

    # Validate config before restarting
    try:
        click.echo(f"Validating configuration from {config_path}...")
        load_config(config_path)
        click.secho("Configuration is valid.", fg="green")
    except Exception as e:
        click.secho(f"Configuration validation failed: {e}", fg="red")
        sys.exit(1)

    # Determine restart mode
    if is_system:
        is_user = False
        if get_uid() != 0:
            click.echo("System-wide restart requires root privileges. Please run with sudo or use --user.")
            sys.exit(1)

    if is_user:
        systemctl_cmd = ["systemctl", "--user"]
    else:
        systemctl_cmd = ["systemctl"]

    try:
        click.echo(f"Restarting {service_name}.service...")
        subprocess.run(systemctl_cmd + ["restart", f"{service_name}.service"], check=True)
        click.secho(f"Successfully restarted {service_name}.service", fg="green")
    except subprocess.CalledProcessError as e:
        click.secho(f"Failed to restart service: {e}", fg="red")
        sys.exit(1)
    except Exception as e:
        click.secho(f"An error occurred: {e}", fg="red")
        sys.exit(1)
