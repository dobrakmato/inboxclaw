import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock

from src.database import Base, Event
from src.pipeline.notifier import EventNotifier
from src.services import AppServices
from src.sinks import win11toast as sink_module
from src.sinks.win11toast import Win11ToastSink


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


def test_win11toast_sink_filters_and_notifies(services):
    sink = Win11ToastSink("toast", {"match": "google.*"}, services)
    sink._toast_available = True

    with services.db_session_maker() as session:
        session.add(
            Event(
                event_id="e1",
                source_id=1,
                event_type="google.drive.file_created",
                entity_id="file_1",
                data={"title": "Quarterly report", "status": "created"},
            )
        )
        session.add(
            Event(
                event_id="e2",
                source_id=1,
                event_type="gmail.message_received",
                entity_id="msg_1",
                data={"subject": "Hello"},
            )
        )
        session.commit()

    calls: list[tuple[str, str]] = []

    def fake_toast(title: str, body: str):
        calls.append((title, body))

    old_toast = sink_module.win11_toast
    sink_module.win11_toast = fake_toast
    try:
        new_last = sink.process_new_events(last_id=0)
    finally:
        sink_module.win11_toast = old_toast

    assert new_last == 2
    assert len(calls) == 1
    assert calls[0][0] == "google.drive.file_created"
    assert "title=Quarterly report" in calls[0][1]


def test_win11toast_sink_summary_fallback_json_snippet(services):
    sink = Win11ToastSink("toast", {"match": "*", "max_body_length": 70}, services)
    sink._toast_available = True

    with services.db_session_maker() as session:
        session.add(
            Event(
                event_id="e1",
                source_id=1,
                event_type="complex.payload",
                entity_id="entity-9",
                data={"deep": {"nested": [{"x": [1, 2, 3]}]}},
            )
        )
        session.commit()

    captured: list[str] = []

    def fake_toast(title: str, body: str):
        assert title == "complex.payload"
        captured.append(body)

    old_toast = sink_module.win11_toast
    sink_module.win11_toast = fake_toast
    try:
        sink.process_new_events(last_id=0)
    finally:
        sink_module.win11_toast = old_toast

    assert len(captured) == 1
    assert "entity=entity-9" in captured[0]
    assert "data=" in captured[0]
    assert len(captured[0]) <= 70


@pytest.mark.asyncio
async def test_win11toast_sink_start_logs_once_when_unavailable(services, caplog):
    sink = Win11ToastSink("toast", {"match": "*"}, services)
    sink._toast_available = False

    add_task_mock = AsyncMock()
    sink.services.add_task = add_task_mock

    with caplog.at_level("WARNING"):
        await sink.start()

    assert sink._task is None
    assert not add_task_mock.called
    assert "is disabled: win11toast import failed" in caplog.text