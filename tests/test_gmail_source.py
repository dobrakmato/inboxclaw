import pytest
from unittest.mock import MagicMock, patch
from src.sources.gmail import GmailSource
from src.config import GmailSourceConfig
import asyncio

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.writer = MagicMock()
    return services

@pytest.mark.asyncio
async def test_gmail_source_filtering(mock_services):
    config = GmailSourceConfig(
        token_file="fake_token.json",
        poll_interval="1m",
        exclude_label_ids=["SPAM", "TRASH"]
    )
    
    source = GmailSource("test_gmail", config, mock_services, source_id=1)
    
    # Mock Gmail Service
    mock_service = MagicMock()
    source._get_service = MagicMock(return_value=mock_service)
    
    # Mock history().list()
    mock_service.users().history().list.return_value.execute.return_value = {
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": "msg1"}},
                    {"message": {"id": "msg2"}},
                    {"message": {"id": "msg3"}},
                ]
            }
        ],
        "historyId": "123"
    }
    
    # Mock messages().get()
    def mock_get_message(userId, id, format, metadataHeaders=None):
        if id == "msg1":
            return {"id": "msg1", "labelIds": ["INBOX", "UNREAD"], "payload": {"headers": []}}
        elif id == "msg2":
            return {"id": "msg2", "labelIds": ["SPAM"], "payload": {"headers": []}}
        elif id == "msg3":
            return {"id": "msg3", "labelIds": ["INBOX", "TRASH"], "payload": {"headers": []}}
        return None

    mock_service.users().messages().get.side_effect = lambda **kwargs: MagicMock(execute=lambda: mock_get_message(**kwargs))
    
    # Mock cursor
    source.cursor.get_last_cursor = MagicMock(return_value="100")
    source.cursor.set_cursor = MagicMock()
    
    await source.fetch_and_publish()
    
    # Check that only msg1 was published
    mock_services.writer.write_events.assert_called_once()
    args, _ = mock_services.writer.write_events.call_args
    source_id, events = args
    assert source_id == 1
    assert len(events) == 1
    assert events[0].event_id == "msg1"
    assert events[0].event_type == "gmail.message_received"

@pytest.mark.asyncio
async def test_gmail_source_sent_emails(mock_services):
    config = GmailSourceConfig(
        token_file="fake_token.json",
        poll_interval="1m"
    )
    
    source = GmailSource("test_gmail", config, mock_services, source_id=1)
    
    # Mock Gmail Service
    mock_service = MagicMock()
    source._get_service = MagicMock(return_value=mock_service)
    
    # Mock history().list()
    mock_service.users().history().list.return_value.execute.return_value = {
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": "msg_sent"}},
                    {"message": {"id": "msg_received"}},
                ]
            }
        ],
        "historyId": "123"
    }
    
    # Mock messages().get()
    def mock_get_message(userId, id, format, metadataHeaders=None):
        if id == "msg_sent":
            return {"id": "msg_sent", "labelIds": ["SENT"], "payload": {"headers": []}}
        elif id == "msg_received":
            return {"id": "msg_received", "labelIds": ["INBOX"], "payload": {"headers": []}}
        return None

    mock_service.users().messages().get.side_effect = lambda **kwargs: MagicMock(execute=lambda: mock_get_message(**kwargs))
    
    # Mock cursor
    source.cursor.get_last_cursor = MagicMock(return_value="100")
    source.cursor.set_cursor = MagicMock()
    
    await source.fetch_and_publish()
    
    # Check both events were published with correct types
    mock_services.writer.write_events.assert_called_once()
    args, _ = mock_services.writer.write_events.call_args
    _, events = args
    assert len(events) == 2
    
    sent_ev = next(e for e in events if e.event_id == "msg_sent")
    assert sent_ev.event_type == "gmail.message_sent"
    
    recv_ev = next(e for e in events if e.event_id == "msg_received")
    assert recv_ev.event_type == "gmail.message_received"

@pytest.mark.asyncio
async def test_gmail_source_other_events(mock_services):
    config = GmailSourceConfig(token_file="fake_token.json")
    source = GmailSource("test_gmail", config, mock_services, source_id=1)
    
    mock_service = MagicMock()
    source._get_service = MagicMock(return_value=mock_service)
    
    mock_service.users().history().list.return_value.execute.return_value = {
        "history": [
            {
                "id": "124",
                "messagesDeleted": [{"message": {"id": "msg_del", "threadId": "t1"}}],
                "labelsAdded": [{"message": {"id": "msg_lab", "threadId": "t2", "labelIds": ["INBOX", "MY_LAB"]}, "labelIds": ["MY_LAB"]}],
                "labelsRemoved": [{"message": {"id": "msg_rem", "threadId": "t3", "labelIds": ["INBOX"]}, "labelIds": ["OLD_LAB"]}]
            }
        ],
        "historyId": "124"
    }
    
    source.cursor.get_last_cursor = MagicMock(return_value="123")
    source.cursor.set_cursor = MagicMock()
    
    await source.fetch_and_publish()
    
    args, _ = mock_services.writer.write_events.call_args
    _, events = args
    assert len(events) == 3
    
    types = [e.event_type for e in events]
    assert "gmail.message_deleted" in types
    assert "gmail.label_added" in types
    assert "gmail.label_removed" in types
    
    del_ev = next(e for e in events if e.event_type == "gmail.message_deleted")
    assert del_ev.event_id == "msg_del-deleted"
    assert del_ev.data["threadId"] == "t1"
    
    lab_ev = next(e for e in events if e.event_type == "gmail.label_added")
    assert lab_ev.event_id == "msg_lab-124-lab-add"
    assert lab_ev.data["labelIds"] == ["MY_LAB"]
    assert lab_ev.data["allLabelIds"] == ["INBOX", "MY_LAB"]

@pytest.mark.asyncio
async def test_gmail_source_default_filtering(mock_services):
    # Default config should exclude SPAM
    config = GmailSourceConfig(
        token_file="fake_token.json"
    )
    assert config.exclude_label_ids == ["SPAM"]
    
    source = GmailSource("test_gmail", config, mock_services, source_id=1)
    
    # Mock Gmail Service
    mock_service = MagicMock()
    source._get_service = MagicMock(return_value=mock_service)
    
    # Mock history().list()
    mock_service.users().history().list.return_value.execute.return_value = {
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": "msg1"}},
                    {"message": {"id": "msg2"}},
                ]
            }
        ],
        "historyId": "123"
    }
    
    # Mock messages().get()
    def mock_get_message(userId, id, format, metadataHeaders=None):
        if id == "msg1":
            return {"id": "msg1", "labelIds": ["INBOX"], "payload": {"headers": []}}
        elif id == "msg2":
            return {"id": "msg2", "labelIds": ["SPAM"], "payload": {"headers": []}}
        return None

    mock_service.users().messages().get.side_effect = lambda **kwargs: MagicMock(execute=lambda: mock_get_message(**kwargs))
    
    # Mock cursor
    source.cursor.get_last_cursor = MagicMock(return_value="100")
    source.cursor.set_cursor = MagicMock()
    
    await source.fetch_and_publish()
    
    # Check that only msg1 was published
    args, _ = mock_services.writer.write_events.call_args
    _, events = args
    assert len(events) == 1
    assert events[0].event_id == "msg1"
