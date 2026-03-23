import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from src.services import AppServices
from src.database import Base, Event, CommandSinkDelivery, Source, Sink
from src.sinks.command import CommandSink
from src.config import Config, DatabaseConfig, CommandSinkConfig
from src.schemas import EventWithMeta
from src.pipeline.notifier import EventNotifier
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

@pytest.fixture
def db_session_maker():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session

@pytest.fixture
def services(db_session_maker):
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

@pytest.fixture
def sink_id(db_session_maker):
    with db_session_maker() as session:
        sink = Sink(name="command_test", type="command")
        session.add(sink)
        session.commit()
        return sink.id

@pytest.fixture
def source_id(db_session_maker):
    with db_session_maker() as session:
        source = Source(name="test_source", type="mock")
        session.add(source)
        session.commit()
        return source.id

@pytest.mark.asyncio
async def test_command_sink_realtime_processing(services, sink_id, source_id):
    # Setup: Create sink
    config = CommandSinkConfig(
        type="command",
        command="echo worked",
    )
    sink = CommandSink("test_sink", config, services, sink_id)
    sink.start()

    # Create an event and notify
    with services.db_session_maker() as session:
        event = Event(
            event_id="e1",
            source_id=source_id,
            event_type="test.event",
            data={"key": "value"},
            created_at=datetime.now(timezone.utc)
        )
        session.add(event)
        session.commit()
        event_id = event.id

    # Emit notify via notifier
    services.notifier.notify()

    # Wait for processor
    await asyncio.sleep(1)

    # Verify DB record
    with services.db_session_maker() as session:
        delivery = session.scalar(
            select(CommandSinkDelivery).where(
                CommandSinkDelivery.event_id == event_id,
                CommandSinkDelivery.sink_id == sink_id
            )
        )
        assert delivery is not None
        assert delivery.processed is True
        assert delivery.return_code == 0
        assert delivery.tries == 1

@pytest.mark.asyncio
async def test_command_sink_retries(services, sink_id, source_id):
    # Setup: Sink that fails
    config = CommandSinkConfig(
        type="command",
        command="exit 1",
        max_retries=2,
        retry_interval=0.1
    )
    sink = CommandSink("test_sink", config, services, sink_id)
    sink.start()

    with services.db_session_maker() as session:
        event = Event(
            event_id="e_retry",
            source_id=source_id,
            event_type="test.event",
            data={},
            created_at=datetime.now(timezone.utc)
        )
        session.add(event)
        session.commit()
        event_id = event.id

    services.notifier.notify()
    
    # Wait for first attempt
    await asyncio.sleep(0.5)
    
    with services.db_session_maker() as session:
        delivery = session.scalar(select(CommandSinkDelivery).where(CommandSinkDelivery.event_id == event_id))
        assert delivery.tries == 1
        assert delivery.processed is False

    # Wait for retry loop or manually trigger it
    await sink._queue_pending_events()
    await asyncio.sleep(0.5)

    with services.db_session_maker() as session:
        delivery = session.scalar(select(CommandSinkDelivery).where(CommandSinkDelivery.event_id == event_id))
        assert delivery.tries == 2
        assert delivery.processed is False

@pytest.mark.asyncio
async def test_command_sink_circuit_breaker(services, sink_id, source_id):
    # Setup: Sink that always fails
    config = CommandSinkConfig(
        type="command",
        command="exit 1",
    )
    sink = CommandSink("test_sink", config, services, sink_id)
    sink.start()

    # Create 6 events
    event_ids = []
    with services.db_session_maker() as session:
        for i in range(6):
            event = Event(
                event_id=f"breaker_{i}",
                source_id=source_id,
                event_type="test.event",
                data={},
                created_at=datetime.now(timezone.utc)
            )
            session.add(event)
            session.commit()
            event_ids.append(event.id)
        
    services.notifier.notify()

    # Wait for them to be processed
    await asyncio.sleep(2)

    # The first 5 should have tries=1, processed=False
    with services.db_session_maker() as session:
        for i in range(5):
            delivery = session.scalar(select(CommandSinkDelivery).where(CommandSinkDelivery.event_id == event_ids[i]))
            assert delivery is not None
            assert delivery.tries == 1
            assert delivery.processed is False
        
        delivery_6 = session.scalar(select(CommandSinkDelivery).where(CommandSinkDelivery.event_id == event_ids[5]))
        # It might have been queued but processor stopped before processing it
        if delivery_6:
            assert delivery_6.tries == 0
        
        assert sink._consecutive_failures >= 5
        assert sink._breaker_until is not None

@pytest.mark.asyncio
async def test_command_sink_ttl(services, sink_id, source_id):
    now = datetime.now(timezone.utc)
    with services.db_session_maker() as session:
        old_event = Event(
            event_id="old",
            source_id=source_id,
            event_type="test.event",
            data={},
            created_at=now - timedelta(hours=2)
        )
        session.add(old_event)
        session.commit()
        old_id = old_event.id

    config = CommandSinkConfig(
        type="command",
        command="echo worked",
        default_ttl="1h",
    )
    sink = CommandSink("test_sink", config, services, sink_id)
    
    # Run _queue_pending_events - it should NOT queue the old event
    await sink._queue_pending_events()
    
    assert sink.queue.qsize() == 0
    
    with services.db_session_maker() as session:
        delivery = session.scalar(select(CommandSinkDelivery).where(CommandSinkDelivery.event_id == old_id))
        assert delivery is None
