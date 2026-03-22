import logging
import os
import sys
import subprocess
from pathlib import Path

def bootstrap():
    # Detect if we are already in a virtual environment
    in_venv = sys.prefix != sys.base_prefix
    
    # Try to see if core dependencies are already present
    try:
        import click # noqa: F401
        import fastapi # noqa: F401
        import yaml # noqa: F401
        deps_installed = True
    except ImportError:
        deps_installed = False

    # If already in a venv and dependencies are present, we don't need to do anything
    if in_venv and deps_installed:
        return

    # Check for .venv in current directory
    venv_dir = Path.cwd() / ".venv"
    python_bin = venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")

    # If .venv exists but we're not in it, just restart with it
    if venv_dir.exists() and python_bin.exists() and not in_venv:
        print(f"[inboxclaw] Virtual environment found at {venv_dir}. Restarting...")
        os.execv(str(python_bin), [str(python_bin)] + sys.argv)

    # If dependencies are missing or no venv exists, let's set it up
    if not deps_installed:
        print("[inboxclaw] First run detected. Setting up virtual environment...")
        
        # 1. Create venv if it doesn't exist
        if not venv_dir.exists():
            print(f"[inboxclaw] Creating virtual environment in {venv_dir}...")
            subprocess.run([sys.executable, "-m", "venv", ".venv"], check=True)
        
        # 2. Install dependencies (this will also create the 'inboxclaw' script in the venv)
        print("[inboxclaw] Installing dependencies (this may take a minute)...")
        subprocess.run([str(python_bin), "-m", "pip", "install", "-e", "."], check=True)

        # 3. Re-run original command with venv python
        print("[inboxclaw] Setup complete. Restarting in virtual environment...")
        os.execv(str(python_bin), [str(python_bin)] + sys.argv)

if __name__ == "__main__":
    bootstrap()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    from src.cli import cli
    cli()
