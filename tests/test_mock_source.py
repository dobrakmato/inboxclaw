import pytest
import asyncio
from unittest.mock import MagicMock
from src.sources.mock import MockSource
from src.services import AppServices
from sqlalchemy import select, create_engine, StaticPool
from sqlalchemy.orm import sessionmaker
from src.database import Base, Event, Source

@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", # Pure in-memory
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine

@pytest.fixture
def db_session_maker(engine):
    Session = sessionmaker(bind=engine)
    return Session

@pytest.fixture
def mock_services(db_session_maker):
    from src.services import AppServices
    import asyncio
    services = MagicMock(spec=AppServices)
    services.db_session_maker = db_session_maker
    services.notifier = MagicMock()
    services.writer = MagicMock()
    services.cursor = MagicMock()
    services.background_tasks = []
    
    def add_task(coro):
        task = asyncio.create_task(coro)
        services.background_tasks.append(task)
        return task
        
    services.add_task.side_effect = add_task
    return services

@pytest.mark.asyncio
async def test_mock_source_generation(mock_services, db_session_maker):
    # Setup source in DB
    with db_session_maker() as session:
        source = Source(name="test_mock", type="mock")
        session.add(source)
        session.commit()
        source_id = source.id

    # Mock services.writer.write_events to actually write to DB
    def mock_write_events(source_id, new_events):
        with db_session_maker() as session:
            for ne in new_events:
                event = Event(
                    source_id=source_id,
                    event_id=ne.event_id,
                    event_type=ne.event_type,
                    entity_id=ne.entity_id,
                    data=ne.data,
                    occurred_at=ne.occurred_at
                )
                session.add(event)
            session.commit()
            # Notify
            mock_services.notifier.notify()

    mock_services.writer.write_events.side_effect = mock_write_events

    config = {"interval": 0.05}
    source_instance = MockSource("test_mock", config, mock_services, source_id)
    
    # Run for a short time
    await source_instance.start()
    await asyncio.sleep(0.15) # Should generate 3 events (0.05, 0.1, 0.15)
    source_instance.stop()
    
    # Wait for tasks to finish if any
    if mock_services.background_tasks:
        for task in mock_services.background_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*mock_services.background_tasks, return_exceptions=True)
    
    # Test with string interval
    config_str = {"interval": "0.1s"}
    source_str = MockSource("test_mock_str", config_str, mock_services, source_id)
    assert source_str.interval == 0.1
    
    # Check if events were generated in DB
    with db_session_maker() as session:
        events = session.scalars(select(Event).where(Event.source_id == source_id)).all()
        assert len(events) >= 2
        for event in events:
            assert event.event_type == "mock.random_number"
            assert "number" in event.data
            assert 1 <= event.data["number"] <= 100

    # Check if notifier was called
    assert mock_services.notifier.notify.called
