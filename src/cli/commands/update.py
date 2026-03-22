import click
import subprocess
import sys
import os
import logging
from src.cli import cli
from src.utils.paths import get_project_root

logger = logging.getLogger("inboxclaw")

@cli.command()
@click.option("--force", is_flag=True, help="Force update even if no changes are detected.")
def update(force: bool):
    """Update the application from GitHub and install dependencies."""
    project_root = get_project_root()
    logger.info(f"Checking for updates in {project_root}...")
    
    try:
        # Change CWD to project root
        os.chdir(project_root)
        
        # 1. Check if we are in a git repository
        if not os.path.exists(".git"):
            logger.error(f"Directory {project_root} is not a git repository. Cannot update.")
            return

        # 2. Fetch the latest changes from the remote
        subprocess.run(["git", "fetch"], check=True, capture_output=True)
        
        # 3. Compare local and remote branches
        local_branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], 
            text=True
        ).strip()
        
        local_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], 
            text=True
        ).strip()
        
        # Check if the branch has an upstream
        try:
            upstream_branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", f"{local_branch}@{{u}}"],
                text=True,
                stderr=subprocess.PIPE
            ).strip()
        except subprocess.CalledProcessError:
            logger.warning(f"No upstream branch configured for '{local_branch}'. Cannot check for remote updates.")
            if not force:
                return
            upstream_branch = None

        if upstream_branch:
            remote_commit = subprocess.check_output(
                ["git", "rev-parse", upstream_branch], 
                text=True
            ).strip()
            
            if local_commit == remote_commit and not force:
                logger.info("Application is already up to date.")
                return
            
            logger.info(f"Updates found! Local: {local_commit[:7]}, Remote: {remote_commit[:7]}")
        elif force:
            logger.info("No upstream branch found, but force update requested. Continuing...")
        
        # 4. Pull the changes
        logger.info("Pulling latest changes...")
        subprocess.run(["git", "pull"], check=True)
        
        # 5. Install dependencies
        logger.info("Installing dependencies...")
        # Assuming pip is available and we want to install from pyproject.toml
        # Using sys.executable to ensure we use the same environment
        subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], check=True)
        
        logger.info("Update completed successfully.")
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Update failed: {e}")
        if e.stderr:
            logger.error(f"Error output: {e.stderr.decode()}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        sys.exit(1)
