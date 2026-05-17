import json
import os
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock

from src.database import Base, Event, Source
from src.pipeline.notifier import EventNotifier
from src.services import AppServices
from src.sinks.folder import FolderSink


@pytest.fixture
def db_session_maker():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def services(db_session_maker):
    return AppServices(
        app=FastAPI(),
        config=None,
        db_session_maker=db_session_maker,
        notifier=EventNotifier(),
    )


@pytest.fixture
def source_id(services):
    with services.db_session_maker() as session:
        source = Source(name="test_source", type="mock")
        session.add(source)
        session.commit()
        session.refresh(source)
        return source.id


def test_folder_sink_writes_jsonl(services, source_id, tmp_path):
    folder = str(tmp_path / "events")
    sink = FolderSink("test_folder", {"path": folder}, services)

    ts = datetime(2025, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    with services.db_session_maker() as session:
        session.add(
            Event(
                event_id="e1",
                source_id=source_id,
                event_type="gmail.message_received",
                entity_id="msg_1",
                data={"subject": "Hello"},
                created_at=ts,
            )
        )
        session.commit()

    sink.process_new_events(last_id=0)

    filepath = os.path.join(folder, "2025-03-15.jsonl")
    assert os.path.exists(filepath)

    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["event_id"] == "e1"
    assert obj["event_type"] == "gmail.message_received"
    assert obj["data"] == {"subject": "Hello"}


def test_folder_sink_filters_by_match(services, source_id, tmp_path):
    folder = str(tmp_path / "events")
    sink = FolderSink("test_folder", {"path": folder, "match": "gmail.*"}, services)

    ts = datetime(2025, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    with services.db_session_maker() as session:
        session.add(
            Event(
                event_id="e1",
                source_id=source_id,
                event_type="gmail.message_received",
                entity_id="msg_1",
                data={"subject": "Hello"},
                created_at=ts,
            )
        )
        session.add(
            Event(
                event_id="e2",
                source_id=source_id,
                event_type="google.drive.file_created",
                entity_id="file_1",
                data={"title": "Report"},
                created_at=ts,
            )
        )
        session.commit()

    sink.process_new_events(last_id=0)

    filepath = os.path.join(folder, "2025-03-15.jsonl")
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["event_type"] == "gmail.message_received"


def test_folder_sink_creates_separate_files_per_day(services, source_id, tmp_path):
    folder = str(tmp_path / "events")
    sink = FolderSink("test_folder", {"path": folder}, services)

    ts1 = datetime(2025, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    ts2 = datetime(2025, 3, 16, 14, 0, 0, tzinfo=timezone.utc)
    with services.db_session_maker() as session:
        session.add(
            Event(
                event_id="e1",
                source_id=source_id,
                event_type="test.event",
                entity_id="ent_1",
                data={"key": "val1"},
                created_at=ts1,
            )
        )
        session.add(
            Event(
                event_id="e2",
                source_id=source_id,
                event_type="test.event",
                entity_id="ent_2",
                data={"key": "val2"},
                created_at=ts2,
            )
        )
        session.commit()

    sink.process_new_events(last_id=0)

    file1 = os.path.join(folder, "2025-03-15.jsonl")
    file2 = os.path.join(folder, "2025-03-16.jsonl")
    assert os.path.exists(file1)
    assert os.path.exists(file2)

    with open(file1, "r", encoding="utf-8") as f:
        lines1 = f.readlines()
    with open(file2, "r", encoding="utf-8") as f:
        lines2 = f.readlines()

    assert len(lines1) == 1
    assert len(lines2) == 1
    assert json.loads(lines1[0])["event_id"] == "e1"
    assert json.loads(lines2[0])["event_id"] == "e2"


def test_folder_sink_appends_to_existing_file(services, source_id, tmp_path):
    folder = str(tmp_path / "events")
    sink = FolderSink("test_folder", {"path": folder}, services)

    ts = datetime(2025, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    with services.db_session_maker() as session:
        session.add(
            Event(
                event_id="e1",
                source_id=source_id,
                event_type="test.event",
                entity_id="ent_1",
                data={"n": 1},
                created_at=ts,
            )
        )
        session.commit()

    sink.process_new_events(last_id=0)

    with services.db_session_maker() as session:
        session.add(
            Event(
                event_id="e2",
                source_id=source_id,
                event_type="test.event",
                entity_id="ent_2",
                data={"n": 2},
                created_at=ts,
            )
        )
        session.commit()

    sink.process_new_events(last_id=1)

    filepath = os.path.join(folder, "2025-03-15.jsonl")
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) == 2
    assert json.loads(lines[0])["event_id"] == "e1"
    assert json.loads(lines[1])["event_id"] == "e2"


def test_folder_sink_default_match_all(services, source_id, tmp_path):
    folder = str(tmp_path / "events")
    sink = FolderSink("test_folder", {"path": folder}, services)
    assert sink.match == "*"


def test_folder_sink_missing_path_raises(services):
    with pytest.raises(KeyError, match="'path'"):
        FolderSink("test_folder", {}, services)


@pytest.mark.asyncio
async def test_folder_sink_start_creates_directory(services, tmp_path):
    folder = str(tmp_path / "new_output_dir")
    sink = FolderSink("test_folder", {"path": folder}, services)

    add_task_mock = AsyncMock()
    sink.services.add_task = add_task_mock

    await sink.start()
    assert os.path.isdir(folder)


def test_folder_sink_includes_source_info(services, source_id, tmp_path):
    folder = str(tmp_path / "events")
    sink = FolderSink("test_folder", {"path": folder}, services)

    ts = datetime(2025, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    with services.db_session_maker() as session:
        session.add(
            Event(
                event_id="e1",
                source_id=source_id,
                event_type="test.event",
                entity_id="ent_1",
                data={"key": "value"},
                created_at=ts,
            )
        )
        session.commit()

    sink.process_new_events(last_id=0)

    filepath = os.path.join(folder, "2025-03-15.jsonl")
    with open(filepath, "r", encoding="utf-8") as f:
        obj = json.loads(f.readline())

    assert obj["source"]["name"] == "test_source"
    assert obj["source"]["id"] == source_id
