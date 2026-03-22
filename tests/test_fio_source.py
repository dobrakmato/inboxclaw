import asyncio
import pytest
from datetime import datetime, timezone, date, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
from src.sources.fio import FioSource
from src.config import FioSourceConfig
from src.schemas import NewEvent

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.db_session_maker = MagicMock()
    services.cursor = MagicMock()
    services.writer = MagicMock()
    return services

@pytest.fixture
def config():
    return FioSourceConfig(
        type="fio",
        token="test_token",
        poll_interval=60,
        max_days_back=30,
        look_ahead_days=7
    )

@pytest.fixture
def mock_response():
    return {
        "accountStatement": {
            "info": {
                "accountId": "123456789",
                "bankId": "2010",
                "currency": "CZK",
                "closingBalance": 1500.0
            },
            "transactionList": {
                "transaction": [
                    {
                        "column22": {"value": 1111, "id": 22, "name": "ID pohybu"},
                        "column0": {"value": 1710547200000, "id": 0, "name": "Datum"}, # 2024-03-15
                        "column1": {"value": 500.0, "id": 1, "name": "Objem"},
                        "column14": {"value": "CZK", "id": 14, "name": "Měna"},
                        "column8": {"value": "Příjem", "id": 8, "name": "Typ"}
                    },
                    {
                        "column22": {"value": 2222, "id": 22, "name": "ID pohybu"},
                        "column0": {"value": 1710547200000, "id": 0, "name": "Datum"},
                        "column1": {"value": -200.0, "id": 1, "name": "Objem"},
                        "column14": {"value": "CZK", "id": 14, "name": "Měna"},
                        "column8": {"value": "Platba", "id": 8, "name": "Typ"}
                    }
                ]
            }
        }
    }

@pytest.mark.asyncio
async def test_fio_poll_success(mock_services, config, mock_response):
    source = FioSource("test_fio", config, mock_services, 1)
    
    # Mock cursor manager
    mock_services.cursor.get_last_cursor = MagicMock(return_value=None)
    mock_services.cursor.set_cursor = MagicMock()
    
    # Mock writer
    mock_services.writer.write_events = MagicMock()
    
    # Mock httpx response
    mock_httpx_response = MagicMock()
    mock_httpx_response.status_code = 200
    mock_httpx_response.json.return_value = mock_response
    mock_httpx_response.raise_for_status = MagicMock()
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_httpx_response
        
        # We need a stable 'now' for cursor check
        now = datetime.now(timezone.utc)
        with patch("src.sources.fio.datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            mock_datetime.fromtimestamp = datetime.fromtimestamp
            mock_datetime.combine = datetime.combine
            mock_datetime.min = datetime.min
            
            await source.poll()
        
        assert mock_get.called
        assert mock_services.writer.write_events.call_count == 1
        
        # Check events passed to write_events
        call_args = mock_services.writer.write_events.call_args
        source_id, events = call_args.args
        assert source_id == 1
        assert len(events) == 2
        
        # Check first event (Income)
        args1 = events[0]
        assert isinstance(args1, NewEvent)
        assert args1.event_id == "fio_1111"
        assert args1.entity_id == "1111"
        assert args1.event_type == "fio.transaction.income"
        assert args1.data["amount"] == 500.0
        assert args1.data["closing_balance"] == 1500.0
        
        # Check second event (Expense)
        args2 = events[1]
        assert isinstance(args2, NewEvent)
        assert args2.event_id == "fio_2222"
        assert args2.entity_id == "2222"
        assert args2.event_type == "fio.transaction.expense"
        assert args2.data["amount"] == -200.0
        
        # Check cursor update
        mock_services.cursor.set_cursor.assert_called()
        # The latest tx date is 2024-03-15, but it should be max(latest_tx, now.date())
        expected_cursor = max(date(2024, 3, 15), now.date()).isoformat()
        mock_services.cursor.set_cursor.assert_any_call(1, expected_cursor)

@pytest.mark.asyncio
async def test_fio_rate_limiting(mock_services, config):
    source = FioSource("test_fio", config, mock_services, 1)
    mock_services.cursor.get_last_cursor = MagicMock(return_value=None)
    mock_services.cursor.set_cursor = MagicMock()
    mock_services.writer.write_events = MagicMock()

    # First poll
    source.last_poll_time = datetime.now(timezone.utc)
    
    mock_httpx_response = MagicMock()
    mock_httpx_response.status_code = 200
    mock_httpx_response.json.return_value = {"accountStatement": {"info": {}, "transactionList": {"transaction": []}}}
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_httpx_response
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # We need to simulate that time has passed but NOT enough to satisfy MIN_POLL_INTERVAL
            # elapsed = 0.1
            with patch("src.sources.fio.datetime") as mock_datetime:
                # Capture the 'now' that poll() will use
                now_val = datetime.now(timezone.utc)
                # Use side_effect directly, NO return_value assignment which would override it
                # We use a mutable list to track calls because mock_datetime.now.call_count might be weird
                call_tracker = []
                def mock_now(tz=None):
                    call_tracker.append(1)
                    if len(call_tracker) == 1:
                        return now_val + timedelta(seconds=0.1)
                    return now_val + timedelta(seconds=60)
                mock_datetime.now.side_effect = mock_now
                mock_datetime.fromtimestamp = datetime.fromtimestamp
                mock_datetime.combine = datetime.combine
                mock_datetime.min = datetime.min

                # Set last_poll_time to something very recent
                source.last_poll_time = now_val

                await source.poll()

            # Should have slept
            assert mock_sleep.called
            # The exact wait time depends on the internal MIN_POLL_INTERVAL_SECONDS which is 30.0
            # If our mock_now returned now_val + 0.1, wait_time = 30.0 - 0.1 = 29.9
            # If it returned now_val + 29.9, wait_time = 30.0 - 29.9 = 0.1
            args = mock_sleep.call_args.args[0]
            assert 0 < args <= 30

@pytest.mark.asyncio
async def test_fio_409_conflict(mock_services, config):
    source = FioSource("test_fio", config, mock_services, 1)
    mock_services.cursor.get_last_cursor = MagicMock(return_value=None)
    
    mock_httpx_response = MagicMock()
    mock_httpx_response.status_code = 409
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_httpx_response
        
        # Should not raise exception, just log and return
        await source.poll()
        
        assert mock_get.called
        mock_services.writer.write_events.assert_not_called()

@pytest.mark.asyncio
async def test_fio_empty_response(mock_services, config):
    source = FioSource("test_fio", config, mock_services, 1)
    mock_services.cursor.get_last_cursor = MagicMock(return_value="2024-03-10")
    mock_services.cursor.set_cursor = MagicMock()
    
    # Fio API returns transaction: null when no transactions
    response = {
        "accountStatement": {
            "info": {"accountId": "123", "bankId": "2010", "currency": "CZK", "closingBalance": 1000.0},
            "transactionList": {"transaction": None}
        }
    }
    
    mock_httpx_response = MagicMock()
    mock_httpx_response.status_code = 200
    mock_httpx_response.json.return_value = response
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_httpx_response
        
        # Stable now for cursor check
        now = datetime.now(timezone.utc)
        with patch("src.sources.fio.datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            
            await source.poll()
        
        mock_services.writer.write_events.assert_not_called()
        # Should still update cursor to today
        mock_services.cursor.set_cursor.assert_called()
        today = now.date().isoformat()
        mock_services.cursor.set_cursor.assert_called_with(1, today)
