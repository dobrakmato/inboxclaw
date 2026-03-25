import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from src.database import init_db, Event, Source, PendingEvent
from src.services import AppServices
from src.config import Config, DatabaseConfig, ServerConfig
from src.pipeline.notifier import EventNotifier
from src.pipeline.coalescence_service import CoalescenceBackgroundService
from fastapi import FastAPI
from unittest.mock import MagicMock

@pytest.fixture
def session_maker():
    # Use in-memory SQLite for testing
    return init_db(":memory:")

@pytest.fixture
def services(session_maker):
    config = Config(
        server=ServerConfig(),
        database=DatabaseConfig(days=30, db_path=":memory:"),
        sources={},
        sink={}
    )
    notifier = MagicMock(spec=EventNotifier)
    app = FastAPI()
    
    # We MUST NOT use AppServices(app, config, db_session_maker, notifier)
    # because its __post_init__ will create real services that might try to 
    # use the same session_maker or have other side effects.
    # Actually, AppServices is fine if we just want to test CoalescenceBackgroundService,
    # but we need to ensure the source exists in the DB.
    
    services = AppServices(
        app=app,
        config=config,
        db_session_maker=session_maker,
        notifier=notifier
    )
    return services

@pytest.mark.asyncio
async def test_coalescence_service_flush_expired(services, session_maker):
    source_id = 1
    with session_maker() as session:
        # SQLite with PRAGMA foreign_keys=ON requires the source to exist
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    p1_last_seen = (now - timedelta(minutes=1)).replace(tzinfo=None) # SQLite stores without TZ

    with session_maker() as session:
        # Expired: flush_at is in the past
        p1 = PendingEvent(
            source_id=source_id,
            event_type="test.expired",
            entity_id="e1",
            data={"v": 1},
            first_seen_at=now - timedelta(minutes=5),
            last_seen_at=p1_last_seen,
            flush_at=now - timedelta(seconds=10),
            count=5,
            strategy="debounce",
            window_seconds=10
        )
        # Not expired: flush_at is in the future
        p2 = PendingEvent(
            source_id=source_id,
            event_type="test.pending",
            entity_id="e2",
            data={"v": 2},
            first_seen_at=now - timedelta(minutes=5),
            last_seen_at=now - timedelta(minutes=1),
            flush_at=now + timedelta(seconds=10),
            count=3,
            strategy="debounce",
            window_seconds=10
        )
        session.add_all([p1, p2])
        session.commit()

    service = CoalescenceBackgroundService(services)
    await service.flush_expired()

    with session_maker() as session:
        # p1 should be promoted to Event and removed from PendingEvent
        events = session.scalars(select(Event).where(Event.event_type == "test.expired")).all()
        assert len(events) == 1
        e = events[0]
        assert e.entity_id == "e1"
        assert e.data == {"v": 1}
        assert e.meta["coalesced"] is True
        assert e.meta["coalesced_count"] == 5
        assert "first_seen_at" in e.meta
        assert "last_seen_at" in e.meta
        assert e.occurred_at == p1_last_seen
        assert e.event_id.startswith("coalesced:test.expired:e1:")

        # p2 should still be in PendingEvent
        pending = session.scalars(select(PendingEvent)).all()
        assert len(pending) == 1
        assert pending[0].event_type == "test.pending"

        # Notifier should have been called
        services.notifier.notify.assert_called_once()

@pytest.mark.asyncio
async def test_coalescence_service_duplicate_id_fallback(services, session_maker):
    source_id = 1
    now = datetime.now(timezone.utc).replace(microsecond=0)
    
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    with session_maker() as session:
        # Manually create an Event that will conflict with expected coalesced event_id
        # expected id format: coalesced:test.dup:e1:{int(timestamp)}
        first_seen = now - timedelta(minutes=5)
        ts = int(first_seen.timestamp())
        existing_id = f"coalesced:test.dup:e1:{ts}"
        
        session.add(Event(
            event_id=existing_id,
            source_id=source_id,
            event_type="test.dup",
            entity_id="e1",
            data={},
            occurred_at=now
        ))
        
        p1 = PendingEvent(
            source_id=source_id,
            event_type="test.dup",
            entity_id="e1",
            data={"v": 1},
            first_seen_at=first_seen,
            last_seen_at=now - timedelta(minutes=1),
            flush_at=now - timedelta(seconds=10),
            count=1,
            strategy="debounce",
            window_seconds=10
        )
        session.add(p1)
        session.commit()

    service = CoalescenceBackgroundService(services)
    await service.flush_expired()

    with session_maker() as session:
        # Should have 2 events now: the original and the new one with fallback id
        events = session.scalars(select(Event).where(Event.event_type == "test.dup")).all()
        assert len(events) == 2
        ids = [e.event_id for e in events]
        
        # We don't check for exact startswith because of timestamp precision/timezone issues in sqlite vs python
        # But we expect two different IDs
        assert len(set(ids)) == 2
        for eid in ids:
            assert eid.startswith("coalesced:test.dup:e1:")
        
        # PendingEvent should be gone
        assert session.scalar(select(PendingEvent)) is None

@pytest.mark.asyncio
async def test_coalescence_service_run_loop(services, monkeypatch):
    # Test that run() calls flush_expired and sleeps
    service = CoalescenceBackgroundService(services, poll_interval=0.01)
    
    flush_called = 0
    async def mock_flush():
        nonlocal flush_called
        flush_called += 1
        if flush_called >= 2:
            raise asyncio.CancelledError() # Break the loop

    monkeypatch.setattr(service, "flush_expired", mock_flush)

    try:
        await service.run()
    except asyncio.CancelledError:
        pass

    assert flush_called >= 2

@pytest.mark.asyncio
async def test_coalescence_service_flush_single_event(services, session_maker):
    """
    Ensures that for single events (count=1), no coalescence metadata is added.
    """
    source_id = 1
    with session_maker() as session:
        # SQLite with PRAGMA foreign_keys=ON requires the source to exist
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    
    with session_maker() as session:
        # Single event (count=1)
        p1 = PendingEvent(
            source_id=source_id,
            event_type="test.single",
            entity_id="e1",
            data={"v": 1},
            meta={"original": "meta"},
            first_seen_at=now - timedelta(minutes=5),
            last_seen_at=now - timedelta(minutes=1),
            flush_at=now - timedelta(seconds=10),
            count=1,
            strategy="debounce",
            window_seconds=10
        )
        session.add(p1)
        session.commit()

    service = CoalescenceBackgroundService(services)
    await service.flush_expired()

    with session_maker() as session:
        events = session.scalars(select(Event).where(Event.event_type == "test.single")).all()
        assert len(events) == 1
        e = events[0]
        assert e.entity_id == "e1"
        assert e.data == {"v": 1}
        
        # Coalescence metadata should NOT be present
        assert "coalesced" not in e.meta
        assert "coalesced_count" not in e.meta
        assert "first_seen_at" not in e.meta
        assert "last_seen_at" not in e.meta
        
        # Original meta should be preserved
        assert e.meta["original"] == "meta"

        # PendingEvent should be gone
        assert session.scalar(select(PendingEvent)) is None
