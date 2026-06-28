import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from zoneinfo import ZoneInfoNotFoundError

import src.sinks.diary as diary_module
from src.config import DiarySinkConfig, load_config
from src.database import Base, Event, Source
from src.pipeline.notifier import EventNotifier
from src.services import AppServices
from src.sinks.diary import DiarySink


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


@pytest.fixture
def no_symlink_updates(monkeypatch):
    updates: list[tuple[Path, Path]] = []

    def fake_atomic_replace_symlink(link_path: Path, target_path: Path) -> bool:
        updates.append((link_path, target_path))
        return True

    monkeypatch.setattr(diary_module, "atomic_replace_symlink", fake_atomic_replace_symlink)
    return updates


def test_diary_config_loads_defaults(tmp_path, monkeypatch):
    for env_name in [
        "DIARY_LLM_ENDPOINT_URL",
        "OPENAI_BASE_URL",
        "DIARY_LLM_API_KEY",
        "OPENAI_API_KEY",
        "DIARY_LLM_MODEL",
        "OPENAI_MODEL",
        "DIARY_LLM_EFFORT",
    ]:
        monkeypatch.delenv(env_name, raising=False)

    config = DiarySinkConfig(type="diary", path=str(tmp_path))

    assert config.path == str(tmp_path)
    assert config.cutoff_time == "04:00"
    assert config.timezone is None
    assert config.lock_timeout == 30.0
    assert config.max_backfill_days == 3
    assert config.summary_mode == "concat"
    assert config.llm_api_key is None
    assert config.llm_model is None
    assert config.daily_prompt_path is None


def test_diary_llm_config_reads_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DIARY_LLM_ENDPOINT_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("DIARY_LLM_API_KEY", "secret")
    monkeypatch.setenv("DIARY_LLM_MODEL", "memory-model")
    monkeypatch.setenv("DIARY_LLM_EFFORT", "medium")
    monkeypatch.setenv("DIARY_LLM_TIMEOUT", "45s")
    monkeypatch.setenv("DIARY_LLM_MAX_RETRIES", "4")

    config = DiarySinkConfig(type="diary", path=str(tmp_path), summary_mode="llm")

    assert config.llm_endpoint_url == "https://llm.example.test/v1"
    assert config.llm_api_key == "secret"
    assert config.llm_model == "memory-model"
    assert config.llm_effort == "medium"
    assert config.llm_timeout == 45.0
    assert config.llm_max_retries == 4


@pytest.mark.asyncio
async def test_diary_sink_start_disables_on_windows_without_admin(services, tmp_path, monkeypatch, caplog):
    sink = DiarySink("test_diary", {"path": str(tmp_path), "timezone": "UTC"}, services)
    add_task = Mock()

    def fail_reconcile(*args, **kwargs):
        raise AssertionError("reconcile should not run when diary sink is disabled")

    monkeypatch.setattr(diary_module.os, "name", "nt")
    monkeypatch.setattr(diary_module, "is_windows_admin", lambda: False)
    monkeypatch.setattr(sink, "reconcile", fail_reconcile)
    monkeypatch.setattr(sink.services, "add_task", add_task)

    with caplog.at_level(logging.WARNING):
        await sink.start()

    assert sink._task is None
    add_task.assert_not_called()
    assert "disabled" in caplog.text


def test_load_config_supports_diary_sink(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "database": {"retention_days": 30, "db_path": ":memory:"},
                "sources": {},
                "sink": {
                    "journal": {
                        "type": "diary",
                        "path": str(tmp_path / "diary"),
                        "timezone": "UTC",
                        "lock_timeout": "1m",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(str(config_file))

    assert config.sink["journal"].type == "diary"
    assert config.sink["journal"].lock_timeout == 60.0


def test_diary_timezone_defaults_to_utc_and_rejects_local(tmp_path, services):
    default_sink = DiarySink("default_diary", {"path": str(tmp_path / "default")}, services)
    assert default_sink.diary_config.timezone is timezone.utc

    try:
        bratislava_sink = DiarySink(
            "bratislava_diary",
            {"path": str(tmp_path / "bratislava"), "timezone": "Europe/Bratislava"},
            services,
        )
    except ValueError as exc:
        if not isinstance(exc.__cause__, ZoneInfoNotFoundError):
            raise
        pytest.skip("tzdata is not installed in this test environment")
    else:
        assert getattr(bratislava_sink.diary_config.timezone, "key", None) == "Europe/Bratislava"

    with pytest.raises(ValueError, match="local"):
        DiarySink("local_diary", {"path": str(tmp_path / "local"), "timezone": "local"}, services)


def test_diary_sink_writes_raw_entries_using_cutoff_date(services, source_id, tmp_path):
    sink = DiarySink("test_diary", {"path": str(tmp_path), "timezone": "UTC"}, services)

    with services.db_session_maker() as session:
        session.add(
            Event(
                event_id="before-cutoff",
                source_id=source_id,
                event_type="test.event",
                entity_id="one",
                data={"n": 1},
                created_at=datetime(2026, 1, 2, 3, 30, tzinfo=timezone.utc),
            )
        )
        session.add(
            Event(
                event_id="at-cutoff",
                source_id=source_id,
                event_type="test.event",
                entity_id="two",
                data={"n": 2},
                created_at=datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc),
            )
        )
        session.commit()

    sink.process_new_events(last_id=0)

    jan1 = tmp_path / "raw" / "2026-01-01.md"
    jan2 = tmp_path / "raw" / "2026-01-02.md"
    assert jan1.exists()
    assert jan2.exists()
    assert json.loads(jan1.read_text(encoding="utf-8").splitlines()[0])["event_id"] == "before-cutoff"
    assert json.loads(jan2.read_text(encoding="utf-8").splitlines()[0])["event_id"] == "at-cutoff"


def test_reconcile_generates_daily_placeholders(no_symlink_updates, services, tmp_path):
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "2026-01-02.md").write_text("user notes\n", encoding="utf-8")
    sink = DiarySink("test_diary", {"path": str(tmp_path), "timezone": "UTC"}, services)

    sink.reconcile(now=datetime(2026, 1, 3, 4, 0, tzinfo=timezone.utc))

    daily = (tmp_path / "daily" / "2026-01-02.md").read_text(encoding="utf-8")
    assert "<daily summary for 2026-01-01 was missing>" in daily
    assert "user notes" in daily
    assert "<raw events file for 2026-01-02 was missing>" in daily
    assert (tmp_path / "user" / "2026-01-03.md").exists()


@pytest.mark.asyncio
async def test_llm_daily_summary_uses_prompt_override_and_input_artifact(
    no_symlink_updates, services, tmp_path, monkeypatch
):
    prompt_path = tmp_path / "daily-prompt.md"
    prompt_path.write_text("Summarize {{DATE}} and keep {{UNKNOWN}} literal.", encoding="utf-8")
    user_dir = tmp_path / "user"
    raw_dir = tmp_path / "raw"
    user_dir.mkdir()
    raw_dir.mkdir()
    (user_dir / "2026-01-02.md").write_text("intentional note\n", encoding="utf-8")
    (raw_dir / "2026-01-02.md").write_text('{"event": "raw"}\n', encoding="utf-8")
    merge = AsyncMock(return_value="# Daily memory - 2026-01-02\n\nLLM summary\n")
    monkeypatch.setattr(diary_module, "llm_merge", merge)
    sink = DiarySink(
        "test_diary",
        {
            "path": str(tmp_path),
            "timezone": "UTC",
            "summary_mode": "llm",
            "llm_api_key": "key",
            "llm_model": "model",
            "daily_prompt_path": str(prompt_path),
        },
        services,
    )

    await sink.reconcile_async(now=datetime(2026, 1, 3, 4, 0, tzinfo=timezone.utc))

    daily = (tmp_path / "daily" / "2026-01-02.md").read_text(encoding="utf-8")
    assert daily == "# Daily memory - 2026-01-02\n\nLLM summary\n"
    _, prompt, input_artifact = merge.await_args.args
    assert prompt == "Summarize 2026-01-02 and keep {{UNKNOWN}} literal."
    assert "intentional note" in input_artifact
    assert '{"event": "raw"}' in input_artifact
    assert "Previous daily summary (2026-01-01)" in input_artifact


@pytest.mark.asyncio
async def test_llm_prompt_changes_do_not_regenerate_existing_summaries(
    no_symlink_updates, services, tmp_path, monkeypatch
):
    prompt_path = tmp_path / "daily-prompt.md"
    prompt_path.write_text("First {{DATE}}", encoding="utf-8")
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "2026-01-02.md").write_text("note\n", encoding="utf-8")
    merge = AsyncMock(return_value="first summary\n")
    monkeypatch.setattr(diary_module, "llm_merge", merge)
    sink = DiarySink(
        "test_diary",
        {
            "path": str(tmp_path),
            "timezone": "UTC",
            "summary_mode": "llm",
            "llm_api_key": "key",
            "llm_model": "model",
            "daily_prompt_path": str(prompt_path),
        },
        services,
    )
    now = datetime(2026, 1, 3, 4, 0, tzinfo=timezone.utc)

    await sink.reconcile_async(now=now)
    prompt_path.write_text("Changed {{DATE}}", encoding="utf-8")
    await sink.reconcile_async(now=now)

    assert (tmp_path / "daily" / "2026-01-02.md").read_text(encoding="utf-8") == "first summary\n"
    assert merge.await_count == 1


def test_daily_skipped_marker_is_not_retried(no_symlink_updates, services, tmp_path):
    sink = DiarySink("test_diary", {"path": str(tmp_path), "timezone": "UTC"}, services)
    now = datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc)

    sink.reconcile(now=now)
    marker_path = tmp_path / "daily" / "2026-01-01.md"
    assert marker_path.read_text(encoding="utf-8") == (
        "<daily summary for 2026-01-01 was skipped because user notes and raw events were both missing>\n"
    )

    marker_path.write_text("user-edited marker\n", encoding="utf-8")
    sink.reconcile(now=now)

    assert marker_path.read_text(encoding="utf-8") == "user-edited marker\n"


def test_reconcile_limits_daily_backfill_from_old_sources(no_symlink_updates, services, tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "2020-01-01.md").write_text("old event\n", encoding="utf-8")
    sink = DiarySink("test_diary", {"path": str(tmp_path), "timezone": "UTC"}, services)

    sink.reconcile(now=datetime(2026, 1, 10, 4, 0, tzinfo=timezone.utc))

    daily_files = sorted(path.name for path in (tmp_path / "daily").glob("*.md"))
    assert daily_files == ["2026-01-09.md"]


def test_weekly_and_monthly_summaries_consume_expected_artifacts(no_symlink_updates, services, tmp_path):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-01-15.md").write_text("midmonth daily summary\n", encoding="utf-8")
    sink = DiarySink(
        "test_diary",
        {"path": str(tmp_path), "timezone": "UTC", "max_backfill_days": 31},
        services,
    )

    sink.reconcile(now=datetime(2026, 2, 1, 4, 0, tzinfo=timezone.utc))

    weekly = (tmp_path / "weekly" / "2026-W03.md").read_text(encoding="utf-8")
    assert "<weekly summary for 2026-W02 was missing>" in weekly
    assert "<daily summary for 2026-01-12 was missing>" in weekly
    assert "midmonth daily summary" in weekly
    assert "<daily summary for 2026-01-16 was skipped because user notes and raw events were both missing>" in weekly

    monthly = (tmp_path / "monthly" / "2026-01.md").read_text(encoding="utf-8")
    assert "<monthly summary for 2025-12 was missing>" in monthly
    assert "<daily summary for 2026-01-01 was missing>" in monthly
    assert "midmonth daily summary" in monthly
    assert "## Daily summary (2026-01-31)" in monthly


def test_weekly_summary_skips_when_all_daily_inputs_are_missing_or_skipped(no_symlink_updates, services, tmp_path):
    sink = DiarySink(
        "test_diary",
        {"path": str(tmp_path), "timezone": "UTC", "max_backfill_days": 31},
        services,
    )

    sink.reconcile(now=datetime(2026, 2, 2, 4, 0, tzinfo=timezone.utc))

    weekly = (tmp_path / "weekly" / "2026-W05.md").read_text(encoding="utf-8")
    assert weekly == (
        "<weekly summary for 2026-W05 was skipped because all daily summaries were missing or skipped>\n"
    )


def test_monthly_summary_skips_when_all_daily_inputs_are_missing_or_skipped(no_symlink_updates, services, tmp_path):
    sink = DiarySink("test_diary", {"path": str(tmp_path), "timezone": "UTC"}, services)

    sink.reconcile(now=datetime(2026, 2, 1, 4, 0, tzinfo=timezone.utc))

    monthly = (tmp_path / "monthly" / "2026-01.md").read_text(encoding="utf-8")
    assert monthly == (
        "<monthly summary for 2026-01 was skipped because all daily summaries were missing or skipped>\n"
    )


def test_reconcile_fills_missing_daily_gap_in_backfill_window(no_symlink_updates, services, tmp_path):
    daily_dir = tmp_path / "daily"
    raw_dir = tmp_path / "raw"
    daily_dir.mkdir()
    raw_dir.mkdir()
    (daily_dir / "2026-01-01.md").write_text("existing day 1\n", encoding="utf-8")
    (daily_dir / "2026-01-03.md").write_text("existing day 3\n", encoding="utf-8")
    (raw_dir / "2026-01-02.md").write_text("raw day 2\n", encoding="utf-8")
    sink = DiarySink(
        "test_diary",
        {"path": str(tmp_path), "timezone": "UTC", "max_backfill_days": 10},
        services,
    )

    sink.reconcile(now=datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc))

    day2 = (tmp_path / "daily" / "2026-01-02.md").read_text(encoding="utf-8")
    assert "raw day 2" in day2
    assert (tmp_path / "daily" / "2026-01-04.md").exists()


def test_reconcile_updates_convenience_symlinks(services, tmp_path):
    _skip_if_symlink_unsupported(tmp_path)
    sink = DiarySink("test_diary", {"path": str(tmp_path), "timezone": "UTC"}, services)

    sink.reconcile(now=datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc))

    today = tmp_path / "today.md"
    yesterday = tmp_path / "yesterday.md"
    assert today.is_symlink()
    assert yesterday.is_symlink()
    assert today.resolve(strict=False) == (tmp_path / "user" / "2026-01-02.md").resolve(strict=False)
    assert yesterday.resolve(strict=False) == (tmp_path / "daily" / "2026-01-01.md").resolve(strict=False)


def _skip_if_symlink_unsupported(tmp_path: Path) -> None:
    target = tmp_path / "probe-target.md"
    link = tmp_path / "probe-link.md"
    target.write_text("", encoding="utf-8")
    try:
        os.symlink(os.path.relpath(target, start=tmp_path), link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks are not available in this test environment: {exc}")
    else:
        link.unlink()
