from pathlib import Path

def get_project_root() -> Path:
    """Returns the root directory of the project."""
    # This file is in src/utils/paths.py
    # .parent -> src/utils
    # .parent.parent -> src
    # .parent.parent.parent -> project root
    return Path(__file__).resolve().parent.parent.parent
