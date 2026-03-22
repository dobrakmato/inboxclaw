import logging
from src.cli import cli
# Import commands to register them
import src.cli.commands.listen # noqa: F401
import src.cli.commands.google_auth # noqa: F401
import src.cli.commands.google_calendar # noqa: F401
import src.cli.commands.nordigen_connect # noqa: F401
import src.cli.commands.update # noqa: F401

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

if __name__ == "__main__":
    cli()
