import click
import subprocess
import os
import sys

from src.cli import cli

@cli.command()
@click.option("-n", "--lines", default=20, help="Number of log lines to show (default: 20).")
@click.option("-f", "--follow", is_flag=True, help="Follow the logs in real-time.")
@click.option("--service-name", default="inboxclaw", help="Name of the systemd service (default: inboxclaw).")
def logs(lines: int, follow: bool, service_name: str):
    """Print logs from the systemd service."""
    if os.name != 'posix':
        click.echo("Logs from systemd are only supported on Linux/POSIX systems.")
        return

    try:
        # Check if user service exists and is active
        res = subprocess.run(["systemctl", "--user", "is-active", service_name], capture_output=True, text=True)
        is_user_service = res.returncode == 0
        
        cmd = ["journalctl"]
        if is_user_service:
            cmd.append("--user")
            
        cmd.extend(["-u", service_name, "-n", str(lines)])
        
        if follow:
            cmd.append("-f")
            
        # Execute journalctl, allowing it to take over the terminal (especially for -f)
        subprocess.run(cmd)
        
    except FileNotFoundError:
        click.secho("Error: 'journalctl' or 'systemctl' command not found. Are you on a systemd-based Linux distribution?", fg="red")
    except Exception as e:
        click.secho(f"Error retrieving logs: {e}", fg="red")
