import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.sources.faktury_online import FakturyOnlineSource
from src.config import FakturyOnlineSourceConfig
from src.schemas import NewEvent

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.kv = MagicMock()
    services.writer = MagicMock()
    services.writer.write_events = MagicMock()
    return services

@pytest.fixture
def config():
    return FakturyOnlineSourceConfig(
        type="faktury_online",
        api_key="test_key",
        email="test@example.com",
        poll_interval=60
    )

@pytest.mark.asyncio
async def test_faktury_online_initial_poll_no_events(mock_services, config):
    source = FakturyOnlineSource("test_faktury", config, mock_services, 1)
    
    # Mock session init
    with patch.object(source, '_init_session', return_value=True):
        # Mock fetch invoices (return one invoice)
        invoice_list = [{"code": "code1"}]
        with patch.object(source, '_fetch_invoices', return_value=invoice_list):
            # Mock get invoice detail
            detail = {"status": 1, "invoice_number": "2024-001", "supplier": "My Corp"}
            with patch.object(source, '_get_invoice_detail', return_value=detail):
                # KV is empty (initialized = None)
                mock_services.kv.get.side_effect = lambda sid, key: None
                
                await source.poll()
                
                # Check if event was NOT written
                mock_services.writer.write_events.assert_not_called()
                
                # Check cache set for the invoice and for initialized flag
                mock_services.kv.set.assert_any_call(1, "invoice:code1", detail)
                mock_services.kv.set.assert_any_call(1, "initialized", True)

@pytest.mark.asyncio
async def test_faktury_online_created(mock_services, config):
    source = FakturyOnlineSource("test_faktury", config, mock_services, 1)
    
    # Mock session init
    with patch.object(source, '_init_session', return_value=True) as mock_init:
        # Mock fetch invoices (return one invoice)
        invoice_list = [{"code": "code1"}]
        with patch.object(source, '_fetch_invoices', return_value=invoice_list):
            # Mock get invoice detail
            detail = {"status": 1, "invoice_number": "2024-001", "supplier": "My Corp"}
            with patch.object(source, '_get_invoice_detail', return_value=detail):
                # No cached snapshot, but ALREADY INITIALIZED
                def mock_kv_get(sid, key):
                    if key == "initialized": return True
                    return None
                mock_services.kv.get.side_effect = mock_kv_get
                
                await source.poll()
                
                # Check if event was written
                mock_services.writer.write_events.assert_called_once()
                source_id, events = mock_services.writer.write_events.call_args.args
                assert source_id == 1
                assert len(events) == 1
                assert isinstance(events[0], NewEvent)
                assert events[0].event_type == "faktury.invoice.created"
                assert events[0].entity_id == "code1"
                assert events[0].data["invoice"] == detail
                
                # Check cache set
                mock_services.kv.set.assert_any_call(1, "invoice:code1", detail)

@pytest.mark.asyncio
async def test_faktury_online_updated(mock_services, config):
    source = FakturyOnlineSource("test_faktury", config, mock_services, 1)
    
    # Mock session init
    with patch.object(source, '_init_session', return_value=True):
        # Mock fetch invoices
        invoice_list = [{"code": "code1"}]
        with patch.object(source, '_fetch_invoices', return_value=invoice_list):
            # Mock get invoice detail (new state)
            new_detail = {"status": 1, "invoice_number": "2024-001", "invoice_paid": "ano"}
            with patch.object(source, '_get_invoice_detail', return_value=new_detail):
                # Cached snapshot (old state)
                old_detail = {"status": 1, "invoice_number": "2024-001", "invoice_paid": "nie"}
                
                def mock_kv_get(sid, key):
                    if key == "invoice:code1": return old_detail
                    if key == "active_codes": return ["code1"]
                    if key == "initialized": return True
                    return None
                
                mock_services.kv.get.side_effect = mock_kv_get
                
                await source.poll()
                
                # Check if updated event was written
                mock_services.writer.write_events.assert_called_once()
                _, events = mock_services.writer.write_events.call_args.args
                event = events[0]
                assert event.event_type == "faktury.invoice.updated"
                assert "changes" in event.data
                assert event.data["changes"]["invoice_paid"]["before"] == "nie"
                assert event.data["changes"]["invoice_paid"]["after"] == "ano"



@pytest.mark.asyncio
async def test_run_accepts_string_poll_interval(mock_services):
    config = FakturyOnlineSourceConfig(
        type="faktury_online",
        api_key="test_key",
        email="test@example.com",
        poll_interval="10s",
    )
    source = FakturyOnlineSource("test_faktury", config, mock_services, 1)

    with patch.object(source, "poll", new=AsyncMock()) as poll_mock:
        with patch("src.sources.faktury_online.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep_mock:
            with pytest.raises(asyncio.CancelledError):
                await source.run()

    poll_mock.assert_awaited_once()
    sleep_mock.assert_awaited_once_with(10.0)


@pytest.mark.asyncio
async def test_run_accepts_default_poll_interval(mock_services):
    config = FakturyOnlineSourceConfig(
        type="faktury_online",
        api_key="test_key",
        email="test@example.com",
    )
    source = FakturyOnlineSource("test_faktury", config, mock_services, 1)

    with patch.object(source, "poll", new=AsyncMock()) as poll_mock:
        with patch("src.sources.faktury_online.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep_mock:
            with pytest.raises(asyncio.CancelledError):
                await source.run()

    poll_mock.assert_awaited_once()
    sleep_mock.assert_awaited_once_with(21600.0)
