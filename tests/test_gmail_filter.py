import pytest
from unittest.mock import MagicMock, patch
from src.sources.gmail import GmailSource
from src.config import GmailSourceConfig, GmailFilterItem

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.writer = MagicMock()
    return services

def test_gmail_source_filtering_logic(mock_services):
    config = GmailSourceConfig(
        token_file="fake_token.json",
        filters=[
            {"ignore_me": GmailFilterItem(in_field="subject", contains="IGNORE ME")},
            {"urgent_test": GmailFilterItem(in_field="subject", regex=r"^\[Urgent\].*test$")},
            {"secret_code": GmailFilterItem(in_field="snippet", contains="SECRET_CODE")},
            {"spam_eggs": GmailFilterItem(in_field="snippet", regex=r"spam.*eggs")},
            {"sender_check": GmailFilterItem(in_field="sender", contains="bot@example.com")}
        ]
    )
    source = GmailSource("test_gmail", config, mock_services, 1)

    # 1. Matches subject contains (case-insensitive)
    msg1 = {
        "payload": {"headers": [{"name": "Subject", "value": "Please ignore me now"}]},
        "snippet": "some body"
    }
    assert source._should_filter(msg1) is True

    # 2. Matches subject regex
    msg2 = {
        "payload": {"headers": [{"name": "Subject", "value": "[Urgent] this is a test"}]},
        "snippet": "some body"
    }
    assert source._should_filter(msg2) is True

    # 3. Matches snippet contains
    msg3 = {
        "payload": {"headers": [{"name": "Subject", "value": "Normal Subject"}]},
        "snippet": "This is a snippet with SECRET_CODE in it"
    }
    assert source._should_filter(msg3) is True

    # 4. Matches snippet regex
    msg4 = {
        "payload": {"headers": [{"name": "Subject", "value": "Normal Subject"}]},
        "snippet": "i like spam and eggs for breakfast"
    }
    assert source._should_filter(msg4) is True

    # 5. Matches sender contains
    msg5 = {
        "payload": {"headers": [
            {"name": "Subject", "value": "Normal Subject"},
            {"name": "From", "value": "Service Bot <bot@example.com>"}
        ]},
        "snippet": "just a regular email"
    }
    assert source._should_filter(msg5) is True

    # 6. No match
    msg6 = {
        "payload": {"headers": [
            {"name": "Subject", "value": "Normal Subject"},
            {"name": "From", "value": "Human <human@example.com>"}
        ]},
        "snippet": "just a regular email"
    }
    assert source._should_filter(msg6) is False

def test_gmail_source_filtering_edge_cases(mock_services):
    # Test case-insensitivity explicitly
    config = GmailSourceConfig(
        token_file="fake_token.json",
        filters=[{"case": GmailFilterItem(in_field="subject", contains="uRgEnT")}]
    )
    source = GmailSource("test_gmail", config, mock_services, 1)
    
    msg_case = {"payload": {"headers": [{"name": "Subject", "value": "THIS IS URGENT!"}]}}
    assert source._should_filter(msg_case) is True

    # Test missing fields
    msg_missing = {"payload": {"headers": []}} # No subject, no from, no snippet
    assert source._should_filter(msg_missing) is False

    # Test empty snippet
    config_snippet = GmailSourceConfig(
        token_file="fake_token.json",
        filters=[{"snip": GmailFilterItem(in_field="snippet", contains="foo")}]
    )
    source_snippet = GmailSource("test_gmail", config_snippet, mock_services, 1)
    assert source_snippet._should_filter({"payload": {"headers": []}}) is False

    # Test missing payload/headers entirely
    assert source_snippet._should_filter({}) is False

    # Test multiple matches
    config_multi = GmailSourceConfig(
        token_file="fake_token.json",
        filters=[
            {"f1": GmailFilterItem(in_field="subject", contains="A")},
            {"f2": GmailFilterItem(in_field="snippet", contains="B")}
        ]
    )
    source_multi = GmailSource("test_gmail", config_multi, mock_services, 1)
    msg_multi = {
        "payload": {"headers": [{"name": "Subject", "value": "A"}]},
        "snippet": "B"
    }
    assert source_multi._should_filter(msg_multi) is True

@patch("src.sources.gmail.build")
@patch("src.sources.gmail.get_google_credentials")
@patch("src.pipeline.cursor.SourceCursor.get_last_cursor")
@patch("src.pipeline.cursor.SourceCursor.set_cursor")
@pytest.mark.asyncio
async def test_gmail_fetch_and_publish_with_filters(
    mock_set_cursor, mock_get_last_cursor, mock_creds, mock_build, mock_services
):
    config = GmailSourceConfig(
        token_file="fake_token.json",
        filters=[{"skip": GmailFilterItem(in_field="subject", contains="SKIP")}]
    )
    source = GmailSource("test_gmail", config, mock_services, 1)
    
    mock_get_last_cursor.return_value = "1000"
    
    service = MagicMock()
    mock_build.return_value = service
    
    # Mock history.list
    service.users().history().list().execute.return_value = {
        "history": [
            {
                "id": "1001",
                "messagesAdded": [
                    {"message": {"id": "msg_skip", "labelIds": ["INBOX"]}},
                    {"message": {"id": "msg_keep", "labelIds": ["INBOX"]}}
                ]
            }
        ],
        "historyId": "1001"
    }
    
    # Mock messages.get
    def mock_get_message(userId, id, format, metadataHeaders=None):
        if id == "msg_skip":
            return {
                "id": "msg_skip",
                "labelIds": ["INBOX"],
                "payload": {"headers": [{"name": "Subject", "value": "Please SKIP this"}]},
                "snippet": "snippet"
            }
        else:
            return {
                "id": "msg_keep",
                "labelIds": ["INBOX"],
                "payload": {"headers": [{"name": "Subject", "value": "Keep this"}]},
                "snippet": "snippet"
            }
            
    service.users().messages().get.side_effect = lambda userId, id, format, metadataHeaders: MagicMock(execute=lambda: mock_get_message(userId, id, format, metadataHeaders))

    await source.fetch_and_publish()
    
    # Verify only one event was written
    mock_services.writer.write_events.assert_called_once()
    events = mock_services.writer.write_events.call_args[0][1]
    assert len(events) == 1
    assert events[0].entity_id == "msg_keep"
