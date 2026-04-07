import os
import click

from src.cli import cli
from src.config import load_config
from src.utils.paths import get_project_root


class MutuallyExclusiveOption(click.Option):
    """A click Option that is mutually exclusive with another option."""

    def __init__(self, *args, **kwargs):
        self.mutually_exclusive = set(kwargs.pop("mutually_exclusive", []))
        super().__init__(*args, **kwargs)

    def handle_parse_result(self, ctx, opts, args):
        if self.name in opts and opts[self.name]:
            for other in self.mutually_exclusive:
                if other in opts and opts[other]:
                    raise click.UsageError(
                        f"--{self.name} and --{other} are mutually exclusive."
                    )
        return super().handle_parse_result(ctx, opts, args)


def _resolve_config_path() -> str:
    """Resolve the config.yaml path, checking CWD first then project root."""
    if os.path.exists("config.yaml"):
        return os.path.abspath("config.yaml")
    return str(get_project_root() / "config.yaml")


@cli.command()
@click.option("--vim", is_flag=True, default=False, help="Open config in vim.",
              cls=MutuallyExclusiveOption, mutually_exclusive=["nano"])
@click.option("--nano", is_flag=True, default=False, help="Open config in nano.",
              cls=MutuallyExclusiveOption, mutually_exclusive=["vim"])
def config(vim: bool, nano: bool):
    """Open the configuration file in your editor."""
    config_path = _resolve_config_path()

    if not os.path.isfile(config_path):
        raise click.ClickException(f"Config file not found: {config_path}")

    # Record modification time before editing
    mtime_before = os.path.getmtime(config_path)

    kwargs = {"filename": config_path}
    if vim:
        kwargs["editor"] = "vim"
    elif nano:
        kwargs["editor"] = "nano"

    click.edit(**kwargs)

    # Check if the file was modified
    mtime_after = os.path.getmtime(config_path)
    if mtime_after > mtime_before:
        click.echo("Config file changed. Validating...")
        try:
            load_config(config_path)
            click.secho("Configuration is valid.", fg="green")
            click.echo("\nYou likely want to restart Inboxclaw to apply changes.")
            click.secho("Run: inboxclaw restart", fg="yellow")
        except Exception as e:
            click.secho(f"Configuration validation failed: {e}", fg="red")
