import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone
from src.sources.google_drive import GoogleDriveSource
from src.config import GoogleDriveSourceConfig, DriveFilterItem

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.writer = MagicMock()
    services.kv = MagicMock()
    return services

def test_google_drive_source_filtering_logic(mock_services):
    config = GoogleDriveSourceConfig(
        token_file="fake_token.json",
        filters=[
            {"ignore_id": DriveFilterItem(in_field="file_id", contains="IGNORE_ME")},
            {"regex_id": DriveFilterItem(in_field="file_id", regex=r"^f[0-9]+$")},
            {"name_check": DriveFilterItem(in_field="name", contains="secret")}
        ]
    )
    source = GoogleDriveSource("test_drive", config, mock_services, 1)

    # 1. Matches file_id contains
    assert source._should_filter("file_IGNORE_ME_123", "normal name") is True

    # 2. Matches file_id regex
    assert source._should_filter("f123", "normal name") is True

    # 3. Matches name contains
    assert source._should_filter("file456", "this is a secret file") is True

    # 4. No match
    assert source._should_filter("file456", "normal file") is False

@pytest.mark.asyncio
async def test_google_drive_process_change_filtering(mock_services):
    config = GoogleDriveSourceConfig(
        token_file="fake_token.json",
        filters=[{"skip": DriveFilterItem(in_field="file_id", contains="SKIP")}]
    )
    source = GoogleDriveSource("test_drive", config, mock_services, 1)
    
    source._fetch_file = AsyncMock(return_value={
        "id": "file_SKIP_123",
        "name": "Filtered File",
        "mimeType": "text/plain",
        "modifiedTime": "2023-01-01T00:00:00Z",
        "version": "1"
    })
    
    change = {"fileId": "file_SKIP_123", "removed": False, "time": "2023-01-01T00:00:00Z"}
    now = datetime.now(timezone.utc)
    
    events = await source._process_change(MagicMock(), change, now)
    
    assert len(events) == 0
    # Ensure it didn't even try to classify or build events
    mock_services.writer.write_events.assert_not_called()
