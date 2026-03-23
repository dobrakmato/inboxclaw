import asyncio
import pytest
from datetime import datetime, timezone
from src.sinks.command import CommandSink
from src.config import Config, DatabaseConfig, CommandSinkConfig
from src.database import Base, Event, Source, Sink
from src.pipeline.notifier import EventNotifier
from src.services import AppServices
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch, MagicMock

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
async def test_command_sink_list_format_robust(services, sink_id, source_id):
    # Test that list format bypasses shell and handles special characters correctly
    # Use a command that would fail if interpreted by shell incorrectly or if split
    
    special_str = "special !@#$%^&*()_+-=[]{}|;':\",./<>? `~"
    
    config = CommandSinkConfig(
        type="command",
        command=[
            "python", 
            "-c", 
            "import sys; print(sys.argv[1]); sys.exit(0)", 
            "Argument with spaces and #root.data.key"
        ],
    )
    
    sink = CommandSink("test_sink", config, services, sink_id)
    
    with services.db_session_maker() as session:
        event = Event(
            event_id="e1",
            source_id=source_id,
            event_type="test.event",
            data={"key": special_str},
            created_at=datetime.now(timezone.utc)
        )
        session.add(event)
        session.commit()
        event_id = event.id

    # We want to verify what exactly is passed to create_subprocess_exec
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        # Mock the process
        mock_process = MagicMock()
        mock_process.communicate.return_value = asyncio.Future()
        mock_process.communicate.return_value.set_result((b"stdout", b"stderr"))
        mock_process.returncode = 0
        mock_exec.return_value = mock_process
        
        await sink._process_one_id(event_id)
        
        # Check that it was called with the right arguments
        assert mock_exec.called
        args, kwargs = mock_exec.call_args
        
        # The 4th argument should be the interpolated string
        # "Argument with spaces and $root.data.key" -> "Argument with spaces and special !@#..."
        expected_arg = f"Argument with spaces and {special_str}"
        assert args[3] == expected_arg
        
        # Verify NO shell quoting was applied (shlex.quote would add single quotes)
        assert "'" not in args[3] or special_str in args[3] # it shouldn't be wrapped in '' unless they are part of special_str

@pytest.mark.asyncio
async def test_command_sink_list_format_json_robust(services, sink_id, source_id):
    # Test that $root (JSON) in list format is passed correctly without shell quoting
    
    config = CommandSinkConfig(
        type="command",
        command=[
            "my_cmd",
            "--payload",
            "$root"
        ],
    )
    
    sink = CommandSink("test_sink", config, services, sink_id)
    
    with services.db_session_maker() as session:
        event = Event(
            event_id="e1",
            source_id=source_id,
            event_type="test.event",
            data={"foo": "bar baz"},
            created_at=datetime.now(timezone.utc)
        )
        session.add(event)
        session.commit()
        event_id = event.id

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_process = MagicMock()
        mock_process.communicate.return_value = asyncio.Future()
        mock_process.communicate.return_value.set_result((b"stdout", b"stderr"))
        mock_process.returncode = 0
        mock_exec.return_value = mock_process
        
        await sink._process_one_id(event_id)
        
        assert mock_exec.called
        args, kwargs = mock_exec.call_args
        
        # The 3rd argument should be the JSON string of the event
        payload = args[2]
        import json
        payload_data = json.loads(payload)
        assert payload_data["data"]["foo"] == "bar baz"
        
        # CRITICAL: It should NOT be shell quoted
        # If it was shell quoted, it would start with ' and end with '
        assert not (payload.startswith("'") and payload.endswith("'"))
