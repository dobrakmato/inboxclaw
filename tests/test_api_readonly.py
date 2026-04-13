import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timezone, timedelta

from src.database import Base, Event, PendingEvent, Source, Sink
from src.services import AppServices
from src.pipeline.notifier import EventNotifier
from src.api_readonly import router as readonly_router


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_session_maker(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def services(db_session_maker):
    app = FastAPI()
    app.include_router(readonly_router)
    svc = AppServices(
        app=app,
        config=None,
        db_session_maker=db_session_maker,
        notifier=EventNotifier(),
    )
    app.state.services = svc
    return svc


@pytest.fixture
def client(services):
    return TestClient(services.app)


@pytest.fixture
def seed_data(db_session_maker):
    """Seed the database with sources, sinks, and events."""
    with db_session_maker() as session:
        src1 = Source(name="gmail", type="gmail")
        src2 = Source(name="calendar", type="google_calendar")
        session.add_all([src1, src2])
        session.commit()
        session.refresh(src1)
        session.refresh(src2)

        sink1 = Sink(name="my_webhook", type="webhook")
        sink2 = Sink(name="my_pull", type="http_pull")
        session.add_all([sink1, sink2])
        session.commit()

        now = datetime.now(timezone.utc)
        events = []
        for i in range(5):
            events.append(Event(
                event_id=f"gmail_{i}",
                source_id=src1.id,
                event_type="mail.new",
                entity_id=f"msg_{i}",
                created_at=now - timedelta(minutes=5 - i),
                data={"subject": f"Test email {i}"},
                meta={"label": "inbox"},
            ))
        for i in range(3):
            events.append(Event(
                event_id=f"cal_{i}",
                source_id=src2.id,
                event_type="calendar.new_event",
                entity_id=f"evt_{i}",
                created_at=now - timedelta(minutes=3 - i),
                data={"title": f"Meeting {i}"},
            ))
        session.add_all(events)
        session.commit()

        # Add a pending event
        session.add(PendingEvent(
            source_id=src1.id,
            event_type="mail.new",
            entity_id="msg_pending",
            data={"subject": "Pending email"},
            count=3,
            first_seen_at=now - timedelta(minutes=10),
            last_seen_at=now - timedelta(minutes=1),
            flush_at=now + timedelta(minutes=5),
            strategy="debounce",
            window_seconds=300,
        ))
        session.commit()

        src1_id = src1.id
        src2_id = src2.id

    return {"source1_id": src1_id, "source2_id": src2_id}


# --- GET /api/events/{id} ---

def test_get_event_by_id(client, seed_data):
    response = client.get("/api/events/1")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 1
    assert data["event_id"] == "gmail_0"
    assert data["source_name"] == "gmail"
    assert data["data"]["subject"] == "Test email 0"


def test_get_event_by_id_not_found(client, seed_data):
    response = client.get("/api/events/9999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Event not found"


# --- GET /api/events ---

def test_get_events_all(client, seed_data):
    response = client.get("/api/events")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 8
    assert len(data["events"]) == 8


def test_get_events_by_ids(client, seed_data):
    response = client.get("/api/events?ids=1,2,3")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["events"]) == 3
    returned_ids = {e["id"] for e in data["events"]}
    assert returned_ids == {1, 2, 3}


def test_get_events_by_ids_invalid(client, seed_data):
    response = client.get("/api/events?ids=abc,xyz")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["events"] == []


def test_get_events_filter_event_type(client, seed_data):
    response = client.get("/api/events?event_type=calendar.new_event")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert all(e["event_type"] == "calendar.new_event" for e in data["events"])


def test_get_events_filter_source_name(client, seed_data):
    response = client.get("/api/events?source_name=gmail")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    assert all(e["source_name"] == "gmail" for e in data["events"])


def test_get_events_pagination(client, seed_data):
    response = client.get("/api/events?limit=3&offset=0")
    data = response.json()
    assert len(data["events"]) == 3
    assert data["total"] == 8

    response2 = client.get("/api/events?limit=3&offset=3")
    data2 = response2.json()
    assert len(data2["events"]) == 3

    # No overlap
    ids1 = {e["id"] for e in data["events"]}
    ids2 = {e["id"] for e in data2["events"]}
    assert ids1.isdisjoint(ids2)


def test_get_events_empty(client, db_session_maker):
    """No seed data — should return empty."""
    # Need at least a source for the join to work, but no events
    response = client.get("/api/events")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["events"] == []


# --- GET /api/events/recent ---

def test_get_recent_events(client, seed_data):
    response = client.get("/api/events/recent?limit=3")
    assert response.status_code == 200
    data = response.json()
    assert len(data["events"]) == 3
    assert data["total"] == 8
    # Should be ordered by created_at desc
    dates = [e["created_at"] for e in data["events"]]
    assert dates == sorted(dates, reverse=True)


def test_get_recent_events_default_limit(client, seed_data):
    response = client.get("/api/events/recent")
    assert response.status_code == 200
    data = response.json()
    # Default limit is 20, we have 8 events
    assert len(data["events"]) == 8


# --- GET /api/pending-events ---

def test_get_pending_events(client, seed_data):
    response = client.get("/api/pending-events")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["events"]) == 1
    pe = data["events"][0]
    assert pe["event_type"] == "mail.new"
    assert pe["entity_id"] == "msg_pending"
    assert pe["count"] == 3
    assert pe["strategy"] == "debounce"
    assert pe["window_seconds"] == 300


def test_get_pending_events_empty(client, db_session_maker):
    response = client.get("/api/pending-events")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["events"] == []


# --- GET /api/sources ---

def test_list_sources(client, seed_data):
    response = client.get("/api/sources")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    names = {s["name"] for s in data}
    assert names == {"gmail", "calendar"}


def test_list_sources_empty(client, db_session_maker):
    response = client.get("/api/sources")
    assert response.status_code == 200
    assert response.json() == []


# --- GET /api/sinks ---

def test_list_sinks(client, seed_data):
    response = client.get("/api/sinks")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    names = {s["name"] for s in data}
    assert names == {"my_webhook", "my_pull"}


def test_list_sinks_empty(client, db_session_maker):
    response = client.get("/api/sinks")
    assert response.status_code == 200
    assert response.json() == []
