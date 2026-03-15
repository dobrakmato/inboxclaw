import pytest
import httpx
from unittest.mock import AsyncMock, patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timezone, timedelta
import asyncio

from src.database import Base, Event, Source, Sink, HttpWebhookDelivery
from src.sinks.webhook import WebhookSink
from src.services import AppServices
from src.pipeline.notifier import EventNotifier
from src.config import Config, DatabaseConfig

@pytest.fixture
def db_session_maker():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session

@pytest.fixture
def sink_id(db_session_maker):
    with db_session_maker() as session:
        sink = Sink(name="test_webhook", type="webhook")
        session.add(sink)
        session.commit()
        return sink.id

@pytest.fixture
def services(db_session_maker):
    # Minimal config for AppServices
    config = Config(
        database=DatabaseConfig(db_path=":memory:"),
        sources={},
        sink={}
    )
    return AppServices(
        app=None,
        config=config,
        db_session_maker=db_session_maker,
        notifier=EventNotifier()
    )

@pytest.mark.asyncio
async def test_webhook_sink_ttl_filtering(services, db_session_maker, sink_id):
    # Setup source
    with db_session_maker() as session:
        source = Source(name="test_source", type="test")
        session.add(source)
        session.commit()
        source_id = source.id

        now = datetime.now(timezone.utc)
        
        # 1. Recent event (matches default TTL)
        session.add(Event(
            event_id="evt_recent",
            source_id=source_id,
            event_type="test.recent",
            entity_id="1",
            created_at=now - timedelta(minutes=5)
        ))
        
        # 2. Old event (exceeds default TTL of 1h)
        session.add(Event(
            event_id="evt_old",
            source_id=source_id,
            event_type="test.old",
            entity_id="2",
            created_at=now - timedelta(hours=2)
        ))
        
        # 3. Old but specific TTL event (exceeds default but matches specific 3h TTL)
        session.add(Event(
            event_id="evt_specific",
            source_id=source_id,
            event_type="important.event",
            entity_id="3",
            created_at=now - timedelta(hours=2)
        ))
        
        # 4. Old specific TTL event (exceeds even the specific 3h TTL)
        session.add(Event(
            event_id="evt_very_old_specific",
            source_id=source_id,
            event_type="important.event",
            entity_id="4",
            created_at=now - timedelta(hours=4)
        ))
        
        session.commit()

    sink_config = {
        "url": "http://example.com/webhook",
        "ttl_enabled": True,
        "default_ttl": "1h",
        "event_ttl": {
            "important.*": "3h"
        }
    }
    sink = WebhookSink("test_sink", sink_config, services, sink_id)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200)
        
        await sink.process_pending_events()
        
        # Should deliver: evt_recent, evt_specific
        # Should NOT deliver: evt_old (exceeds 1h), evt_very_old_specific (exceeds 3h)
        assert mock_post.call_count == 2
        
        delivered_event_ids = [call.kwargs["json"]["event_id"] for call in mock_post.call_args_list]
        assert "evt_recent" in delivered_event_ids
        assert "evt_specific" in delivered_event_ids
        assert "evt_old" not in delivered_event_ids
        assert "evt_very_old_specific" not in delivered_event_ids

@pytest.mark.asyncio
async def test_webhook_sink_ttl_disabled(services, db_session_maker, sink_id):
    with db_session_maker() as session:
        source = Source(name="test_source", type="test")
        session.add(source)
        session.commit()
        
        # Very old event
        session.add(Event(
            event_id="evt_very_old",
            source_id=source.id,
            event_type="test.type",
            entity_id="1",
            created_at=datetime.now(timezone.utc) - timedelta(days=10)
        ))
        session.commit()

    sink_config = {
        "url": "http://example.com/webhook",
        "ttl_enabled": False,
        "default_ttl": "1h"
    }
    sink = WebhookSink("test_sink", sink_config, services, sink_id)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200)
        await sink.process_pending_events()
        
        # Should deliver despite being very old because TTL is disabled
        assert mock_post.call_count == 1
        assert mock_post.call_args.kwargs["json"]["event_id"] == "evt_very_old"
