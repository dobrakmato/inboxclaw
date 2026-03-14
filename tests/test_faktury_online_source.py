import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from src.sources.faktury_online import FakturyOnlineSource
from src.config import FakturyOnlineSourceConfig

@pytest.fixture
def mock_services():
    services = MagicMock()
    services.kv = MagicMock()
    services.writer = MagicMock()
    services.writer.write_event = AsyncMock()
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
                # No cached snapshot
                mock_services.kv.get.side_effect = lambda sid, key: None if "invoice:code1" in key else []
                
                await source.poll()
                
                # Check if event was written
                mock_services.writer.write_event.assert_called()
                args, kwargs = mock_services.writer.write_event.call_args
                assert kwargs["event_type"] == "faktury.invoice.created"
                assert kwargs["entity_id"] == "code1"
                assert kwargs["data"]["invoice"] == detail
                
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
                    return None
                
                mock_services.kv.get.side_effect = mock_kv_get
                
                await source.poll()
                
                # Check if updated event was written
                mock_services.writer.write_event.assert_called()
                args, kwargs = mock_services.writer.write_event.call_args
                assert kwargs["event_type"] == "faktury.invoice.updated"
                assert "changes" in kwargs["data"]
                assert kwargs["data"]["changes"]["invoice_paid"]["before"] == "nie"
                assert kwargs["data"]["changes"]["invoice_paid"]["after"] == "ano"

@pytest.mark.asyncio
async def test_faktury_online_deleted(mock_services, config):
    source = FakturyOnlineSource("test_faktury", config, mock_services, 1)
    
    # Mock session init
    with patch.object(source, '_init_session', return_value=True):
        # Mock fetch invoices (empty list, one was there before)
        with patch.object(source, '_fetch_invoices', return_value=[]):
            
            def mock_kv_get(sid, key):
                if key == "active_codes": return ["code1"]
                if key == "invoice:code1": return {"invoice_number": "2024-001"}
                return None
            
            mock_services.kv.get.side_effect = mock_kv_get
            
            await source.poll()
            
            # Check if deleted event was written
            mock_services.writer.write_event.assert_called()
            args, kwargs = mock_services.writer.write_event.call_args
            assert kwargs["event_type"] == "faktury.invoice.deleted"
            assert kwargs["entity_id"] == "code1"
            
            # Check cache delete
            mock_services.kv.delete.assert_any_call(1, "invoice:code1")
