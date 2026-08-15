from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from src.config import (
    CoalesceRule,
    CoalesceStrategy,
    Config,
    DatabaseConfig,
    GoogleDriveSourceConfig,
    ServerConfig,
)
from src.database import Event, PendingEvent, Source, SourceKV, init_db
from src.schemas import NewEvent
from src.services import AppServices
from src.sources.google_drive import DriveCacheMutation, GoogleDriveSource
from src.utils.google_drive_sync import DriveFileSnapshot


@pytest.fixture
def transactional_source(tmp_path):
    session_maker = init_db(str(tmp_path / "drive-transaction.db"))
    drive_config = GoogleDriveSourceConfig(
        token_file="token.json",
        coalesce=[
            CoalesceRule(
                match="google.drive.file_updated",
                strategy=CoalesceStrategy.DEBOUNCE,
                window="60s",
            )
        ],
    )
    config = Config(
        server=ServerConfig(),
        database=DatabaseConfig(days=30, db_path=":memory:"),
        sources={"drive": drive_config},
        sink={},
    )
    notifier = MagicMock()
    services = AppServices(
        app=FastAPI(),
        config=config,
        db_session_maker=session_maker,
        notifier=notifier,
    )
    with session_maker() as session:
        session.add(Source(id=1, name="drive", type="google_drive", cursor="old-token"))
        session.commit()
    return GoogleDriveSource("drive", drive_config, services, source_id=1), services, session_maker


def snapshot() -> DriveFileSnapshot:
    return DriveFileSnapshot(
        file_id="f1",
        name="Doc",
        mime_type="text/plain",
        parents=["root"],
        trashed=False,
        created_time="2026-04-01T00:00:00Z",
        modified_time="2026-04-01T01:00:00Z",
        owned_by_me=True,
        version="2",
    )


def test_page_commit_persists_event_cache_and_cursor_together(transactional_source):
    source, services, session_maker = transactional_source
    event = NewEvent(
        event_id="created-f1",
        event_type="google.drive.file_created",
        entity_id="f1",
        occurred_at=datetime.now(timezone.utc),
        data={"fileId": "f1"},
    )

    source._commit_page(
        [event],
        [DriveCacheMutation("set", "f1", snapshot())],
        "new-token",
    )

    with session_maker() as session:
        assert session.query(Event).filter_by(event_id="created-f1").count() == 1
        assert session.query(SourceKV).filter_by(key=source._snapshot_key("f1")).count() == 1
        assert session.get(Source, 1).cursor == "new-token"
    services.notifier.notify.assert_called_once()


def test_page_commit_rolls_back_coalescing_and_cache_when_cursor_write_fails(
    transactional_source,
):
    source, services, session_maker = transactional_source
    event = NewEvent(
        event_id="updated-f1",
        event_type="google.drive.file_updated",
        entity_id="f1",
        occurred_at=datetime.now(timezone.utc),
        data={"fileId": "f1", "changes": {"name": {"before": "A", "after": "B"}}},
    )
    services.cursor.set_cursor_in_session = MagicMock(side_effect=RuntimeError("cursor failed"))

    with pytest.raises(RuntimeError, match="cursor failed"):
        source._commit_page(
            [event],
            [DriveCacheMutation("set", "f1", snapshot())],
            "new-token",
        )

    with session_maker() as session:
        assert session.query(Event).count() == 0
        assert session.query(PendingEvent).count() == 0
        assert session.query(SourceKV).count() == 0
        assert session.get(Source, 1).cursor == "old-token"
    services.notifier.notify.assert_not_called()


def test_shared_drive_page_commits_event_cache_and_its_checkpoint(transactional_source):
    source, services, session_maker = transactional_source
    event = NewEvent(
        event_id="removed-shared-f1",
        event_type="google.drive.file_removed",
        entity_id="f1",
        occurred_at=datetime.now(timezone.utc),
        data={"fileId": "f1"},
    )
    checkpoint_key = source._shared_drive_cursor_key("drive-1")

    source._commit_page(
        [event],
        [DriveCacheMutation("set", "f1", snapshot())],
        None,
        kv_updates={checkpoint_key: "drive-token"},
    )

    with session_maker() as session:
        assert session.query(Event).filter_by(event_id="removed-shared-f1").count() == 1
        assert session.query(SourceKV).filter_by(key=checkpoint_key).one().value == "drive-token"
        assert session.get(Source, 1).cursor == "old-token"
    services.notifier.notify.assert_called_once()
