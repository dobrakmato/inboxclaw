from pathlib import Path
from threading import Lock
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from src.utils.paths import get_project_root


_TOKEN_LOCKS: dict[Path, Lock] = {}


def _token_lock(token_path: Path) -> Lock:
    resolved = token_path.resolve()
    lock = _TOKEN_LOCKS.get(resolved)
    if lock is None:
        lock = Lock()
        _TOKEN_LOCKS[resolved] = lock
    return lock

def get_google_credentials(token_file: str, source_name: str) -> Credentials:
    """
    Shared logic to load and refresh Google OAuth2 credentials from a token file.
    
    :param token_file: Path to the JSON token file.
    :param source_name: Name of the source for error reporting.
    :return: Refreshed Credentials object.
    :raises ValueError: If token_file is not provided.
    """
    if not token_file:
        raise ValueError(f"No token_file configured for Google source {source_name}")
    
    token_path = Path(token_file)
    if not token_path.is_absolute() and not token_path.exists():
        # Try relative to project root
        project_root = get_project_root()
        alt_path = project_root / token_path
        if alt_path.exists():
            token_path = alt_path
    
    token_path = token_path.resolve()
    creds = Credentials.from_authorized_user_file(str(token_path))

    if creds and creds.expired and creds.refresh_token:
        with _token_lock(token_path):
            creds = Credentials.from_authorized_user_file(str(token_path))
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                tmp_path = token_path.with_name(f"{token_path.name}.tmp")
                tmp_path.write_text(creds.to_json(), encoding="utf-8")
                tmp_path.replace(token_path)

    return creds
