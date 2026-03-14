from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

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
    
    creds = Credentials.from_authorized_user_file(token_file)
    
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    
    return creds
