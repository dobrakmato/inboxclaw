import json

import pytest
from fastapi import FastAPI
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import sessionmaker

from src.config import Config, NordigenSourceConfig
from src.database import Base, Event, Source
from src.health import SourceHealthRegistry
from src.initialization import _health_interval
from src.pipeline.notifier import EventNotifier
from src.services import AppServices


@pytest.fixture
def services():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    config = Config(database={"db_path": ":memory:"}, sources={}, sink={})
    svc = AppServices(
        app=FastAPI(),
        config=config,
        db_session_maker=sessions,
        notifier=EventNotifier(),
    )
    with sessions() as session:
        internal = Source(name="inboxclaw", type="inboxclaw")
        source = Source(name="gmail_primary", type="gmail")
        session.add_all([internal, source])
        session.commit()
        session.refresh(internal)
        session.refresh(source)
        internal_id = internal.id
        source_id = source.id
    svc.health.set_internal_source(internal_id)
    svc.health.register(
        "gmail_primary",
        "gmail",
        source_id,
        expected_interval=60,
    )
    return svc, source_id, internal_id


def test_health_starts_without_reporting_failure(services):
    svc, _, _ = services
    snapshot = svc.health.snapshot()

    assert snapshot["status"] == "starting"
    assert snapshot["failures"] == []
    assert snapshot["sources"][0]["status"] == "starting"


def test_nordigen_watchdog_uses_effective_poll_interval():
    config = NordigenSourceConfig(poll_interval="1m")

    assert _health_interval(config) == config.effective_poll_interval


def test_health_emits_only_on_unhealthy_and_recovered_transitions(services):
    svc, _, internal_id = services
    reporter = svc.health.reporter("gmail_primary")

    reporter.unhealthy("authentication", "The Gmail token was revoked.")
    reporter.unhealthy("authentication", "The Gmail token was revoked again.")

    with svc.db_session_maker() as session:
        events = session.scalars(select(Event).where(Event.source_id == internal_id)).all()
        assert [event.event_type for event in events] == ["inboxclaw.source.unhealthy"]

    reporter.healthy()
    reporter.healthy()

    with svc.db_session_maker() as session:
        events = session.scalars(
            select(Event).where(Event.source_id == internal_id).order_by(Event.id)
        ).all()
        assert [event.event_type for event in events] == [
            "inboxclaw.source.unhealthy",
            "inboxclaw.source.recovered",
        ]


def test_single_transient_failure_does_not_change_health_or_emit(services):
    svc, _, internal_id = services
    reporter = svc.health.reporter("gmail_primary")
    reporter.healthy()

    reporter.unhealthy("connectivity", "The Gmail request failed temporarily.")

    snapshot = svc.health.snapshot()
    source = snapshot["sources"][0]
    assert snapshot["status"] == "healthy"
    assert source["status"] == "healthy"
    assert source["pending_failure"]["code"] == "connectivity"
    assert source["pending_failure"]["consecutive_failures"] == 1
    with svc.db_session_maker() as session:
        events = session.scalars(select(Event).where(Event.source_id == internal_id)).all()
        assert events == []


def test_success_clears_unconfirmed_transient_failure_without_events(services):
    svc, _, internal_id = services
    reporter = svc.health.reporter("gmail_primary")
    reporter.healthy()
    reporter.unhealthy("timeout", "The Gmail request timed out.")

    reporter.healthy()

    source = svc.health.snapshot()["sources"][0]
    assert source["status"] == "healthy"
    assert source["pending_failure"] is None
    with svc.db_session_maker() as session:
        events = session.scalars(select(Event).where(Event.source_id == internal_id)).all()
        assert events == []


def test_two_transient_failures_emit_once_then_one_success_recovers(services):
    svc, _, internal_id = services
    reporter = svc.health.reporter("gmail_primary")
    reporter.healthy()

    reporter.unhealthy("timeout", "The first request timed out.")
    reporter.unhealthy("upstream", "The retry returned HTTP 503.")

    assert svc.health.snapshot()["status"] == "unhealthy"
    reporter.healthy()

    with svc.db_session_maker() as session:
        events = session.scalars(
            select(Event).where(Event.source_id == internal_id).order_by(Event.id)
        ).all()
        assert [event.event_type for event in events] == [
            "inboxclaw.source.unhealthy",
            "inboxclaw.source.recovered",
        ]


def test_actionable_failure_is_immediately_unhealthy(services):
    svc, _, internal_id = services

    svc.health.reporter("gmail_primary").unhealthy(
        "authentication",
        "The Gmail token was revoked.",
    )

    assert svc.health.snapshot()["status"] == "unhealthy"
    with svc.db_session_maker() as session:
        events = session.scalars(select(Event).where(Event.source_id == internal_id)).all()
        assert [event.event_type for event in events] == ["inboxclaw.source.unhealthy"]


def test_notification_latch_survives_registry_recreation(services):
    svc, source_id, internal_id = services
    svc.health.reporter("gmail_primary").unhealthy("authentication", "Token revoked.")

    replacement = SourceHealthRegistry(svc)
    replacement.set_internal_source(internal_id)
    replacement.register("gmail_primary", "gmail", source_id, expected_interval=60)
    replacement.reporter("gmail_primary").unhealthy("authentication", "Still revoked.")

    with svc.db_session_maker() as session:
        events = session.scalars(select(Event).where(Event.source_id == internal_id)).all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_healthcheck_http_status_uses_registry(services, monkeypatch):
    from src import app as app_module

    svc, _, _ = services
    app_module.app.state.services = svc

    response = await app_module.healthcheck()
    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "starting"

    svc.health.reporter("gmail_primary").unhealthy("authentication", "Token revoked.")
    response = await app_module.healthcheck()
    assert response.status_code == 503
    assert json.loads(response.body)["failures"][0]["name"] == "gmail_primary"
