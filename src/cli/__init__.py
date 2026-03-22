import click

@click.group()
def cli():
    """Inboxclaw CLI."""
    pass

# Import commands to register them
import src.cli.commands.listen # noqa: F401
import src.cli.commands.google_auth # noqa: F401
import src.cli.commands.google_calendar # noqa: F401
import src.cli.commands.nordigen_connect # noqa: F401
import src.cli.commands.update # noqa: F401
import src.cli.commands.install # noqa: F401
import src.cli.commands.status # noqa: F401
import src.cli.commands.restart # noqa: F401
import src.cli.commands.subscribe # noqa: F401
import src.cli.commands.pull # noqa: F401
