import click
import subprocess
import os
import sys
from typing import Optional

from src.cli import cli

def get_uid() -> int:
    """Get the current user's UID. Returns -1 on non-POSIX systems."""
    if os.name == 'posix':
        return os.getuid()
    return -1

@cli.command()
@click.option("--service-name", default="inboxclaw", help="Name of the systemd service to restart.")
@click.option("--user", "is_user", is_flag=True, default=True, help="Restart as a user service (default).")
@click.option("--system", "is_system", is_flag=True, help="Restart as a system-wide service (requires root).")
def restart(service_name: str, is_user: bool, is_system: bool):
    """Restart the Inboxclaw systemd service."""
    if os.name != 'posix':
        click.echo("Systemd is only supported on Linux/POSIX systems.")
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
