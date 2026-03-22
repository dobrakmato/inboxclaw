import click
import os
import sys
import getpass
from pathlib import Path
from typing import Optional
import subprocess

from src.cli import cli

SYSTEMD_SERVICE_TEMPLATE = """[Unit]
Description=Inboxclaw Service
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={working_dir}
ExecStart={exec_start}
Restart=always
Environment=PYTHONPATH={working_dir}
{env_vars}

[Install]
WantedBy={wanted_by}
"""

@cli.command()
@click.option("--config", "config_path", default="config.yaml", help="Path to the configuration file.")
@click.option("--user", "is_user", is_flag=True, default=True, help="Install as a user service (default).")
@click.option("--system", "is_system", is_flag=True, help="Install as a system-wide service (requires root).")
@click.option("--name", "service_name", default="inboxclaw", help="Name of the systemd service.")
def install(config_path: str, is_user: bool, is_system: bool, service_name: str):
    """Install Inboxclaw as a systemd service."""
    if os.name != 'posix':
        click.echo("Systemd is only supported on Linux/POSIX systems.")
        sys.exit(1)

    # Determine installation mode
    if is_system:
        is_user = False
        if os.getuid() != 0:
            click.echo("System-wide installation requires root privileges. Please run with sudo or use --user.")
            sys.exit(1)

    current_user = getpass.getuser()
    working_dir = str(Path.cwd().absolute())
    config_abs_path = str(Path(config_path).absolute())
    
    # Identify the python executable and the entry point
    python_exe = sys.executable
    exec_start = f"{python_exe} main.py listen --config {config_abs_path}"

    env_vars = f"Environment=CONFIG_PATH={config_abs_path}"
    
    wanted_by = "default.target" if is_user else "multi-user.target"
    
    service_content = SYSTEMD_SERVICE_TEMPLATE.format(
        user=current_user,
        working_dir=working_dir,
        exec_start=exec_start,
        env_vars=env_vars,
        wanted_by=wanted_by
    )

    if is_user:
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / f"{service_name}.service"
        systemctl_cmd = ["systemctl", "--user"]
    else:
        unit_dir = Path("/etc/systemd/system")
        unit_path = unit_dir / f"{service_name}.service"
        systemctl_cmd = ["systemctl"]

    try:
        unit_path.write_text(service_content)
        click.echo(f"Created service file at {unit_path}")
    except PermissionError:
        click.echo(f"Permission denied while writing to {unit_path}. Try running with sudo.")
        sys.exit(1)

    # Reload systemd, enable and start service
    try:
        subprocess.run(systemctl_cmd + ["daemon-reload"], check=True)
        subprocess.run(systemctl_cmd + ["enable", f"{service_name}.service"], check=True)
        subprocess.run(systemctl_cmd + ["restart", f"{service_name}.service"], check=True)
        
        click.echo(f"Successfully installed and started {service_name}.service")
        
        # Link CLI to PATH
        if is_user:
            bin_dir = Path.home() / ".local" / "bin"
        else:
            bin_dir = Path("/usr/local/bin")
            
        try:
            bin_dir.mkdir(parents=True, exist_ok=True)
            # The executable is in the same directory as sys.executable
            # (e.g. .venv/bin/python -> .venv/bin/inboxclaw)
            python_bin_dir = Path(sys.executable).parent
            inboxclaw_bin = python_bin_dir / "inboxclaw"
            
            target_bin = bin_dir / "inboxclaw"
            
            if inboxclaw_bin.exists():
                if target_bin.exists() or target_bin.is_symlink():
                    target_bin.unlink()
                target_bin.symlink_to(inboxclaw_bin)
                click.echo(f"Added 'inboxclaw' command to {bin_dir}")
                
                # Check if bin_dir is in PATH
                path_dirs = os.environ.get("PATH", "").split(os.pathsep)
                if str(bin_dir) not in path_dirs and str(bin_dir.resolve()) not in path_dirs:
                    click.secho(f"Warning: {bin_dir} is not in your PATH. You may need to add it to your shell profile.", fg="yellow")
            else:
                click.echo(f"Note: 'inboxclaw' executable not found in {python_bin_dir}. Skipping symlink creation.")

        except Exception as e:
            click.echo(f"Failed to create symlink in {bin_dir}: {e}")

        if is_user:
            click.echo("Note: To ensure the service starts on boot without you logging in, run: loginctl enable-linger")
    except subprocess.CalledProcessError as e:
        click.echo(f"Failed to enable/start service: {e}")
        sys.exit(1)

if __name__ == "__main__":
    install()
