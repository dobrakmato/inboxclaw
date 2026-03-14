import pytest
from src.database import init_db, Source
from src.services import AppServices
from src.config import Config, DatabaseConfig, ServerConfig
from src.pipeline.notifier import EventNotifier
from fastapi import FastAPI

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
    notifier = EventNotifier()
    app = FastAPI()
    
    services = AppServices(
        app=app,
        config=config,
        db_session_maker=session_maker,
        notifier=notifier
    )
    return services

def test_source_cursor_operations(services, session_maker):
    cursor_service = services.cursor
    source_id = 1
    
    # Setup: Create a source in the DB
    with session_maker() as session:
        src = Source(id=source_id, name="test_source", type="mock")
        session.add(src)
        session.commit()

    # Initial cursor should be None
    assert cursor_service.get_last_cursor(source_id) is None

    # Set cursor
    cursor_service.set_cursor(source_id, "token_123")
    assert cursor_service.get_last_cursor(source_id) == "token_123"

    # Verify in DB
    with session_maker() as session:
        src_db = session.get(Source, source_id)
        assert src_db.cursor == "token_123"

    # Set cursor again
    cursor_service.set_cursor(source_id, "token_456")
    assert cursor_service.get_last_cursor(source_id) == "token_456"

    # Clear cursor
    cursor_service.set_cursor(source_id, None)
    assert cursor_service.get_last_cursor(source_id) is None

def test_get_last_cursor_non_existent_source(services):
    assert services.cursor.get_last_cursor(999) is None
