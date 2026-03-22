import asyncio
import pytest
import httpx
from unittest.mock import AsyncMock, patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timezone, timedelta

from src.database import Base, Event, Source, HttpWebhookDelivery, Sink
from src.sinks.webhook import WebhookSink

@pytest.fixture
def sink_id(db_session_maker):
    with db_session_maker() as session:
        sink = Sink(name="test_sink", type="webhook")
        session.add(sink)
        session.commit()
        return sink.id
from src.services import AppServices
from src.pipeline.notifier import EventNotifier

@pytest.fixture
def db_session_maker():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session

@pytest.fixture
def services(db_session_maker):
    return AppServices(
        app=None,
        config=None,
        db_session_maker=db_session_maker,
        notifier=EventNotifier()
    )

@pytest.mark.asyncio
async def test_webhook_sink_delivery(services, db_session_maker, sink_id):
    # Setup source
    with db_session_maker() as session:
        source = Source(name="test_source", type="test")
        session.add(source)
        session.commit()
        source_id = source.id

        # Setup event
        event = Event(
            event_id="evt_123",
            source_id=source_id,
            event_type="test.event",
            entity_id="entity_1",
            data={"foo": "bar"}
        )
        session.add(event)
        session.commit()
        event_db_id = event.id

    sink_config = {
        "url": "http://example.com/webhook",
        "match": "test.*"
    }
    sink = WebhookSink("test_sink", sink_config, services, sink_id)

    # Mock httpx.AsyncClient.post
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200, content=b'{"status": "ok"}')

        await sink.process_pending_events()

        # Check if post was called
        assert mock_post.called
        call_args = mock_post.call_args
        assert call_args.args[0] == "http://example.com/webhook"
        payload = call_args.kwargs["json"]
        assert payload["event_id"] == "evt_123"
        assert payload["event_type"] == "test.event"
        assert payload["data"] == {"foo": "bar"}

    # Check database for delivery status
    with db_session_maker() as session:
        delivery = session.scalar(
            select(HttpWebhookDelivery).where(
                (HttpWebhookDelivery.event_id == event_db_id) & (HttpWebhookDelivery.sink_id == sink_id)
            )
        )
        assert delivery is not None
        assert delivery.delivered is True
        assert delivery.tries == 1

@pytest.mark.asyncio
async def test_webhook_sink_retry(services, db_session_maker, sink_id):
    # Setup source and event
    with db_session_maker() as session:
        source = Source(name="test_source", type="test")
        session.add(source)
        session.commit()
        
        event = Event(
            event_id="evt_retry",
            source_id=source.id,
            event_type="test.event",
            entity_id="entity_retry",
            data={}
        )
        session.add(event)
        session.commit()
        event_db_id = event.id

    sink_config = {
        "url": "http://example.com/webhook",
        "max_retries": 2,
        "retry_interval": 0,
    }
    sink = WebhookSink("test_sink", sink_config, services, sink_id)

    # Mock httpx.AsyncClient.post to fail
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(500, content=b"Error")

        # First attempt
        await sink.process_pending_events()
        
        with db_session_maker() as session:
            delivery = session.scalar(select(HttpWebhookDelivery).where(HttpWebhookDelivery.event_id == event_db_id))
            assert delivery.tries == 1
            assert delivery.delivered is False

        # Second attempt
        await sink.process_pending_events()
        
        with db_session_maker() as session:
            delivery = session.scalar(select(HttpWebhookDelivery).where(HttpWebhookDelivery.event_id == event_db_id))
            assert delivery.tries == 2
            assert delivery.delivered is False

        # Third attempt (should not happen because max_retries = 2)
        await sink.process_pending_events()
        
        with db_session_maker() as session:
            delivery = session.scalar(select(HttpWebhookDelivery).where(HttpWebhookDelivery.event_id == event_db_id))
            assert delivery.tries == 2 # Remains 2
            assert delivery.delivered is False


@pytest.mark.asyncio
async def test_webhook_sink_custom_headers(services, db_session_maker, sink_id):
    # Setup source and event
    with db_session_maker() as session:
        source = Source(name="test_source", type="test")
        session.add(source)
        session.commit()
        source_id = source.id

        event = Event(
            event_id="evt_headers",
            source_id=source_id,
            event_type="test.event",
            entity_id="entity_1",
            data={"foo": "bar"}
        )
        session.add(event)
        session.commit()

    sink_config = {
        "url": "http://example.com/webhook",
        "match": "test.*",
        "headers": {
            "Authorization": "Bearer secret-token",
            "X-Custom-Header": "custom-value"
        }
    }
    sink = WebhookSink("test_sink", sink_config, services, sink_id)

    # Mock httpx.AsyncClient.post
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200, content=b'{"status": "ok"}')

        await sink.process_pending_events()

        # Check if post was called with correct headers
        assert mock_post.called
        call_args = mock_post.call_args
        assert call_args.kwargs["headers"] == {
            "Authorization": "Bearer secret-token",
            "X-Custom-Header": "custom-value"
        }


@pytest.mark.asyncio
async def test_webhook_sink_respects_retry_interval(services, db_session_maker, sink_id):
    with db_session_maker() as session:
        source = Source(name="test_source", type="test")
        session.add(source)
        session.commit()

        event = Event(
            event_id="evt_retry_interval",
            source_id=source.id,
            event_type="test.event",
            entity_id="entity_retry_interval",
            data={}
        )
        session.add(event)
        session.commit()
        event_db_id = event.id

    sink = WebhookSink(
        "test_sink",
        {
            "url": "http://example.com/webhook",
            "max_retries": 3,
            "retry_interval": 60,
        },
        services,
        sink_id,
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(500, content=b"Error")

        await sink.process_pending_events()
        await sink.process_pending_events()

        assert mock_post.call_count == 1

        with db_session_maker() as session:
            delivery = session.scalar(select(HttpWebhookDelivery).where(HttpWebhookDelivery.event_id == event_db_id))
            assert delivery.tries == 1

            delivery.last_try = datetime.now(timezone.utc) - timedelta(seconds=61)
            session.commit()

        await sink.process_pending_events()
        assert mock_post.call_count == 2

@pytest.mark.asyncio
async def test_webhook_sink_filtering(services, db_session_maker, sink_id):
    # Setup events with different types
    with db_session_maker() as session:
        source = Source(name="test_source", type="test")
        session.add(source)
        session.commit()
        
        session.add(Event(event_id="evt_match", source_id=source.id, event_type="match.me", entity_id="1"))
        session.add(Event(event_id="evt_no_match", source_id=source.id, event_type="other.type", entity_id="2"))
        session.add(Event(event_id="evt_exact", source_id=source.id, event_type="exact.match", entity_id="3"))
        session.commit()

    # Test match.*
    sink_config = {
        "url": "http://example.com/webhook",
        "match": "match.*"
    }
    sink = WebhookSink("test_sink", sink_config, services, sink_id)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200)
        await sink.process_pending_events()
        
        # Should only be called once for "match.me"
        assert mock_post.call_count == 1
        payload = mock_post.call_args.kwargs["json"]
        assert payload["event_id"] == "evt_match"

    # Test exact match
    sink.match = "exact.match"
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200)
        await sink.process_pending_events()
        assert mock_post.call_count == 1
        payload = mock_post.call_args.kwargs["json"]
        assert payload["event_id"] == "evt_exact"

    # Test match everything with list containing "*"
    sink.match = ["other.type", "*"]
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200)
        await sink.process_pending_events()
        # All 3 events should be sent if not already delivered.
        # However, due to how the test is structured, and since they are in the same DB,
        # they might have been marked as delivered in previous steps.
        # Let's check delivered status
        with db_session_maker() as session:
             deliveries = session.scalars(select(HttpWebhookDelivery).where(HttpWebhookDelivery.delivered == True)).all()
             delivered_event_ids = [d.event_id for d in deliveries]
             
             # Events already delivered: evt_match (1), exact.match (3), and now evt_no_match (2)
             assert len(delivered_event_ids) == 3

def test_webhook_sink_config_error(services, sink_id):
    with pytest.raises(ValueError, match="requires a 'url' configuration"):
        WebhookSink("test", {}, services, sink_id)

@pytest.mark.asyncio
async def test_webhook_sink_start_stop(services, sink_id):
    sink_config = {"url": "http://example.com/webhook"}
    sink = WebhookSink("test_sink", sink_config, services, sink_id)
    
    # Mock _run_loop to avoid actual execution
    with patch.object(sink, "_run_loop", new_callable=AsyncMock) as mock_run_loop:
        await sink.start()
        assert sink._task is not None
        assert not sink._task.done()
        
        # Start again should do nothing
        task = sink._task
        await sink.start()
        assert sink._task is task
        
        await sink.stop()
        assert sink._task is None
        
        # Stop again should do nothing
        await sink.stop()

@pytest.mark.asyncio
async def test_webhook_sink_run_loop_and_exception(services, db_session_maker, sink_id):
    sink_config = {"url": "http://example.com/webhook", "retry_interval": 0.1}
    sink = WebhookSink("test_sink", sink_config, services, sink_id)
    
    with patch.object(sink, "process_pending_events", new_callable=AsyncMock) as mock_process:
        # Mock notifier.subscribe to return an event we can control
        notify_event = asyncio.Event()
        with patch.object(services.notifier, "subscribe", return_value=notify_event):
            # Start the loop in a task
            loop_task = asyncio.create_task(sink._run_loop())
            
            # Give it a moment to run process_pending_events
            await asyncio.sleep(0.05)
            assert mock_process.called
            
            # Trigger notification
            mock_process.reset_mock()
            notify_event.set()
            await asyncio.sleep(0.05)
            assert mock_process.called
            assert not notify_event.is_set() # It should have been cleared
            
            # Test timeout
            mock_process.reset_mock()
            # Just wait for more than retry_interval
            await asyncio.sleep(0.15)
            assert mock_process.called
            
            # Stop the loop
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

    # Test general exception in loop (subscribe fails)
    with patch.object(services.notifier, "subscribe", side_effect=Exception("Subscribe Error")):
        await sink._run_loop()

    # Test general exception in loop (process_pending_events fails)
    with patch.object(services.notifier, "subscribe", return_value=asyncio.Event()):
        with patch.object(sink, "process_pending_events", side_effect=Exception("Process Error")):
            await sink._run_loop()

@pytest.mark.asyncio
async def test_webhook_sink_match_property(services, sink_id):
    sink = WebhookSink("webhook", {"url": "http://test", "match": "test.*"}, services, sink_id)
    assert sink.match == "test.*"
    
    # Test setter
    sink.match = ["a", "b"]
    assert sink.match == ["a", "b"]
    
    sink.match = "single"
    assert sink.match == "single"

@pytest.mark.asyncio
async def test_webhook_sink_delivery_exception(services, db_session_maker, sink_id):
    with db_session_maker() as session:
        source = Source(name="test_source", type="test")
        session.add(source)
        session.commit()
        session.add(Event(event_id="evt_exc", source_id=source.id, event_type="test", entity_id="1"))
        session.commit()

    sink_config = {"url": "http://example.com/webhook"}
    sink = WebhookSink("test_sink", sink_config, services, sink_id)

    with patch("httpx.AsyncClient.post", side_effect=httpx.RequestError("Network error")):
        await sink.process_pending_events()
        # Should catch exception and log it
        
    with db_session_maker() as session:
        delivery = session.scalar(select(HttpWebhookDelivery))
        assert delivery.tries == 1
        assert delivery.delivered is False

@pytest.mark.asyncio
async def test_webhook_sink_payload_rewriting(services, db_session_maker, sink_id):
    # Setup source
    with db_session_maker() as session:
        source = Source(name="test_source", type="test")
        session.add(source)
        session.commit()
        source_id = source.id

        # Setup event
        event = Event(
            event_id="evt_123",
            source_id=source_id,
            event_type="test.event",
            entity_id="entity_1",
            data={
                "browser": "Chrome",
                "nested": {"key": "value"}
            }
        )
        session.add(event)
        session.commit()

    sink_config = {
        "url": "http://example.com/webhook",
        "payload": {
            "key_a": 1,
            "key_b": "static",
            "key_c": "#root.event_id",
            "key_d": "#root.source.name",
            "key_e": {
                "key_1": "#root.data.browser",
                "key_2": "#root.data.nested",
                "key_3": "$root.data.nested"
            }
        }
    }
    sink = WebhookSink("test_sink", sink_config, services, sink_id)

    # Mock httpx.AsyncClient.post
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200, content=b'{"status": "ok"}')

        await sink.process_pending_events()

        assert mock_post.called
        call_args = mock_post.call_args
        payload = call_args.kwargs["json"]

        assert payload["key_a"] == 1
        assert payload["key_b"] == "static"
        assert payload["key_c"] == "evt_123"
        assert payload["key_d"] == "test_source"
        assert payload["key_e"]["key_1"] == "Chrome"
        assert payload["key_e"]["key_2"] == {"key": "value"}
        assert payload["key_e"]["key_3"] == '{"key": "value"}'

@pytest.mark.asyncio
async def test_webhook_sink_string_interpolation_advanced(services, db_session_maker, sink_id):
    # Setup source and event
    with db_session_maker() as session:
        source = Source(name="test_source_interp", type="test")
        session.add(source)
        session.commit()

        event = Event(
            event_id="evt_789",
            source_id=source.id,
            event_type="test.event",
            entity_id="entity_789",
            data={"foo": "bar", "num": 123}
        )
        session.add(event)
        session.commit()

    sink_config = {
        "url": "http://example.com/webhook",
        "payload": {
            "msg": "Event #root.event_id happened with #root.data.foo",
            "full_json": "Data: $root.data",
            "nested": {
                "info": "Source #root.source.name"
            },
            "types": "Num is #root.data.num, string is #root.data.foo",
            "raw_num": "#root.data.num" # Should stay integer
        }
    }
    sink = WebhookSink("test_sink_interp", sink_config, services, sink_id)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200, content=b'{"status": "ok"}')

        await sink.process_pending_events()

        assert mock_post.called
        payload = mock_post.call_args.kwargs["json"]

        assert payload["msg"] == "Event evt_789 happened with bar"
        assert payload["full_json"] == 'Data: {"foo": "bar", "num": 123}'
        assert payload["nested"]["info"] == "Source test_source_interp"
        assert payload["types"] == "Num is 123, string is bar"
        assert payload["raw_num"] == 123 # Important check for type preservation when no other text
