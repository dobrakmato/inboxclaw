import asyncio
import os
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import select, create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from src.config import FilesystemSourceConfig
from src.database import Base, Event, Source
from src.services import AppServices
from src.sources.filesystem import (
    FilesystemSource,
    FileSnapshot,
    _hash_file,
    _content_preview,
    _read_text,
    _matches_any,
    _extension_ok,
    _is_binary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
def mock_services(db_session_maker):
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


@pytest.fixture
def source_id(db_session_maker):
    with db_session_maker() as session:
        source = Source(name="test_fs", type="filesystem")
        session.add(source)
        session.commit()
        return source.id


# ---------------------------------------------------------------------------
# Unit tests — helper functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_hash_file(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        h = _hash_file(f)
        assert h.startswith("sha256:")
        assert len(h) > 10

    def test_hash_file_missing(self, tmp_path):
        assert _hash_file(tmp_path / "nope.txt") == ""

    def test_content_preview(self, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("# Title\nSome content here that is longer than the limit")
        preview = _content_preview(f, 10)
        assert preview == "# Title\nSo"

    def test_content_preview_missing(self, tmp_path):
        assert _content_preview(tmp_path / "nope.md", 100) == ""

    def test_matches_any_simple(self):
        assert _matches_any("file.tmp", ["*.tmp"])
        assert not _matches_any("file.md", ["*.tmp"])

    def test_matches_any_directory_pattern(self):
        assert _matches_any(".git/config", [".git/**"])
        assert _matches_any(".git/objects/pack", [".git/**"])
        assert not _matches_any("pages/note.md", [".git/**"])

    def test_extension_ok(self):
        assert _extension_ok("note.md", [".md", ".org"])
        assert not _extension_ok("image.png", [".md", ".org"])
        assert _extension_ok("anything.xyz", None)  # no filter = allow all

    def test_extension_ok_case_insensitive(self):
        assert _extension_ok("NOTE.MD", [".md"])


# ---------------------------------------------------------------------------
# Unit tests — FilesystemSource scanning & diffing
# ---------------------------------------------------------------------------

class TestFilesystemSourceScanning:
    def _make_source(self, tmp_path, mock_services, source_id, **overrides):
        defaults = {
            "type": "filesystem",
            "path": str(tmp_path),
            "watch_mode": "poll",
            "poll_interval": "1m",
        }
        defaults.update(overrides)
        config = FilesystemSourceConfig(**defaults)
        return FilesystemSource("test_fs", config, mock_services, source_id)

    def test_scan_empty_directory(self, tmp_path, mock_services, source_id):
        src = self._make_source(tmp_path, mock_services, source_id)
        result = src._scan_directory()
        assert result == {}

    def test_scan_finds_files(self, tmp_path, mock_services, source_id):
        (tmp_path / "note.md").write_text("hello")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "deep.txt").write_text("deep")

        src = self._make_source(tmp_path, mock_services, source_id)
        result = src._scan_directory()
        assert "note.md" in result
        assert "sub/deep.txt" in result

    def test_scan_respects_extensions(self, tmp_path, mock_services, source_id):
        (tmp_path / "note.md").write_text("hello")
        (tmp_path / "image.png").write_bytes(b"\x89PNG")

        src = self._make_source(tmp_path, mock_services, source_id, extensions=[".md"])
        result = src._scan_directory()
        assert "note.md" in result
        assert "image.png" not in result

    def test_scan_respects_ignore_patterns(self, tmp_path, mock_services, source_id):
        (tmp_path / "note.md").write_text("hello")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("gitconfig")

        src = self._make_source(tmp_path, mock_services, source_id, ignore_patterns=[".git/**"])
        result = src._scan_directory()
        assert "note.md" in result
        assert ".git/config" not in result

    def test_scan_non_recursive(self, tmp_path, mock_services, source_id):
        (tmp_path / "top.md").write_text("top")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.md").write_text("deep")

        src = self._make_source(tmp_path, mock_services, source_id, recursive=False)
        result = src._scan_directory()
        assert "top.md" in result
        assert "sub/deep.md" not in result

    def test_diff_detects_created(self, tmp_path, mock_services, source_id):
        src = self._make_source(tmp_path, mock_services, source_id)
        # Empty baseline
        src._snapshots = {}
        (tmp_path / "new.md").write_text("new file")
        events = src._full_scan_and_diff()
        assert len(events) == 1
        assert events[0].event_type == "fs.file_created"
        assert events[0].entity_id == "new.md"

    def test_diff_detects_deleted(self, tmp_path, mock_services, source_id):
        f = tmp_path / "old.md"
        f.write_text("old file")
        src = self._make_source(tmp_path, mock_services, source_id)
        # Establish baseline
        src._full_scan_and_diff()
        # Delete the file
        f.unlink()
        events = src._full_scan_and_diff()
        assert len(events) == 1
        assert events[0].event_type == "fs.file_deleted"
        assert events[0].entity_id == "old.md"

    def test_diff_detects_updated(self, tmp_path, mock_services, source_id):
        f = tmp_path / "note.md"
        f.write_text("version 1")
        src = self._make_source(tmp_path, mock_services, source_id)
        src._full_scan_and_diff()
        # Modify
        f.write_text("version 2 with more content")
        events = src._full_scan_and_diff()
        assert len(events) == 1
        assert events[0].event_type == "fs.file_updated"
        assert "content_diff" in events[0].data
        assert events[0].data["content_diff"]["totalChangedSections"] >= 1

    def test_diff_no_change(self, tmp_path, mock_services, source_id):
        (tmp_path / "stable.md").write_text("unchanged")
        src = self._make_source(tmp_path, mock_services, source_id)
        src._full_scan_and_diff()
        events = src._full_scan_and_diff()
        assert events == []


# ---------------------------------------------------------------------------
# Unit tests — event data payload
# ---------------------------------------------------------------------------

class TestEventData:
    def _make_source(self, tmp_path, mock_services, source_id, **overrides):
        defaults = {
            "type": "filesystem",
            "path": str(tmp_path),
            "watch_mode": "poll",
            "poll_interval": "1m",
            "include_content_preview": True,
            "content_preview_length": 50,
        }
        defaults.update(overrides)
        config = FilesystemSourceConfig(**defaults)
        return FilesystemSource("test_fs", config, mock_services, source_id)

    def test_event_data_fields(self, tmp_path, mock_services, source_id):
        (tmp_path / "pages").mkdir()
        (tmp_path / "pages" / "note.md").write_text("# My Note\nSome content")
        src = self._make_source(tmp_path, mock_services, source_id)
        events = src._full_scan_and_diff()
        assert len(events) == 1
        data = events[0].data
        assert data["path"] == "pages/note.md"
        assert data["mime_type"] == "text/markdown"
        assert data["size_bytes"] > 0
        assert "# My Note" in data["content_preview"]
        # Removed internal/redundant fields should NOT be present
        assert "is_binary" not in data
        assert "content_hash" not in data
        # Redundant fields should NOT be present
        assert "file_path" not in data
        assert "file_name" not in data
        assert "extension" not in data
        assert "directory" not in data

    def test_event_data_mime_type_binary(self, tmp_path, mock_services, source_id):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")
        src = self._make_source(tmp_path, mock_services, source_id)
        events = src._full_scan_and_diff()
        data = events[0].data
        assert data["mime_type"] == "image/png"

    def test_event_data_mime_type_unknown(self, tmp_path, mock_services, source_id):
        (tmp_path / "data.xyz123").write_text("something")
        src = self._make_source(tmp_path, mock_services, source_id)
        events = src._full_scan_and_diff()
        data = events[0].data
        assert data["mime_type"] == "application/octet-stream"

    def test_content_preview_disabled(self, tmp_path, mock_services, source_id):
        (tmp_path / "note.md").write_text("content")
        src = self._make_source(tmp_path, mock_services, source_id, include_content_preview=False)
        events = src._full_scan_and_diff()
        assert "content_preview" not in events[0].data


# ---------------------------------------------------------------------------
# Unit tests — binary vs text detection
# ---------------------------------------------------------------------------

class TestBinaryDetection:
    def test_is_binary(self):
        assert _is_binary("image.png")
        assert _is_binary("archive.zip")
        assert _is_binary("doc.pdf")
        assert not _is_binary("note.md")
        assert not _is_binary("script.py")
        assert not _is_binary("data.json")

    def test_read_text_returns_none_for_binary(self, tmp_path):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n")
        assert _read_text(f) is None

    def test_read_text_returns_content_for_text(self, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("hello world")
        assert _read_text(f) == "hello world"

    def test_binary_file_has_no_preview(self, tmp_path, mock_services, source_id):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")
        config = FilesystemSourceConfig(
            type="filesystem", path=str(tmp_path), watch_mode="poll",
            poll_interval="1m", include_content_preview=True,
        )
        src = FilesystemSource("test_fs", config, mock_services, source_id)
        events = src._full_scan_and_diff()
        assert len(events) == 1
        assert "content_preview" not in events[0].data
        assert "is_binary" not in events[0].data

    def test_text_file_has_preview_when_enabled(self, tmp_path, mock_services, source_id):
        (tmp_path / "note.md").write_text("hello")
        config = FilesystemSourceConfig(
            type="filesystem", path=str(tmp_path), watch_mode="poll",
            poll_interval="1m", include_content_preview=True,
        )
        src = FilesystemSource("test_fs", config, mock_services, source_id)
        events = src._full_scan_and_diff()
        assert len(events) == 1
        assert events[0].data["content_preview"] == "hello"
        assert "is_binary" not in events[0].data

    def test_binary_update_no_content_diff(self, tmp_path, mock_services, source_id):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\x00\x00")
        config = FilesystemSourceConfig(type="filesystem", path=str(tmp_path), watch_mode="poll", poll_interval="1m")
        src = FilesystemSource("test_fs", config, mock_services, source_id)
        src._full_scan_and_diff()
        f.write_bytes(b"\x89PNG\x00\x01\x02")
        events = src._full_scan_and_diff()
        assert len(events) == 1
        assert events[0].event_type == "fs.file_updated"
        assert "content_diff" not in events[0].data

    def test_binary_file_no_content_preview(self, tmp_path, mock_services, source_id):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")
        config = FilesystemSourceConfig(
            type="filesystem", path=str(tmp_path), watch_mode="poll",
            poll_interval="1m", include_content_preview=True,
        )
        src = FilesystemSource("test_fs", config, mock_services, source_id)
        events = src._full_scan_and_diff()
        assert "content_preview" not in events[0].data


# ---------------------------------------------------------------------------
# Unit tests — content diff on text file updates
# ---------------------------------------------------------------------------

class TestContentDiff:
    def _make_source(self, tmp_path, mock_services, source_id, **overrides):
        defaults = {
            "type": "filesystem",
            "path": str(tmp_path),
            "watch_mode": "poll",
            "poll_interval": "1m",
        }
        defaults.update(overrides)
        config = FilesystemSourceConfig(**defaults)
        return FilesystemSource("test_fs", config, mock_services, source_id)

    def test_text_update_has_content_diff(self, tmp_path, mock_services, source_id):
        f = tmp_path / "note.md"
        f.write_text("# Hello\n\nFirst paragraph.")
        src = self._make_source(tmp_path, mock_services, source_id)
        src._full_scan_and_diff()
        f.write_text("# Hello\n\nUpdated paragraph.")
        events = src._full_scan_and_diff()
        assert len(events) == 1
        diff = events[0].data["content_diff"]
        assert diff["totalChangedSections"] >= 1
        assert diff["addedCharCount"] > 0
        assert diff["removedCharCount"] > 0
        assert len(diff["changes"]) >= 1

    def test_content_diff_not_present_on_create(self, tmp_path, mock_services, source_id):
        src = self._make_source(tmp_path, mock_services, source_id)
        src._snapshots = {}
        (tmp_path / "new.md").write_text("brand new")
        events = src._full_scan_and_diff()
        assert len(events) == 1
        assert events[0].event_type == "fs.file_created"
        assert "content_diff" not in events[0].data

    def test_content_diff_not_present_on_delete(self, tmp_path, mock_services, source_id):
        f = tmp_path / "old.md"
        f.write_text("old content")
        src = self._make_source(tmp_path, mock_services, source_id)
        src._full_scan_and_diff()
        f.unlink()
        events = src._full_scan_and_diff()
        assert len(events) == 1
        assert events[0].event_type == "fs.file_deleted"
        assert "content_diff" not in events[0].data

    def test_content_diff_respects_max_sections(self, tmp_path, mock_services, source_id):
        f = tmp_path / "note.md"
        # Create many paragraphs
        paragraphs = [f"Paragraph {i} original." for i in range(10)]
        f.write_text("\n\n".join(paragraphs))
        src = self._make_source(tmp_path, mock_services, source_id, max_changed_sections=2)
        src._full_scan_and_diff()
        # Change all paragraphs
        paragraphs = [f"Paragraph {i} changed." for i in range(10)]
        f.write_text("\n\n".join(paragraphs))
        events = src._full_scan_and_diff()
        diff = events[0].data["content_diff"]
        assert len(diff["changes"]) <= 2


# ---------------------------------------------------------------------------
# Integration test — poll mode run loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_mode_run(tmp_path, mock_services, source_id):
    """Test that poll mode detects file creation during the run loop."""
    (tmp_path / "initial.md").write_text("initial")

    config = FilesystemSourceConfig(
        type="filesystem",
        path=str(tmp_path),
        watch_mode="poll",
        poll_interval=0.1,
    )
    src = FilesystemSource("test_fs", config, mock_services, source_id)

    # Run in background
    task = asyncio.create_task(src.run())
    await asyncio.sleep(0.05)  # Let initial scan complete

    # Initial scan should have written events
    assert mock_services.writer.write_events.called
    first_call_events = mock_services.writer.write_events.call_args_list[0][0][1]
    assert any(e.event_type == "fs.file_created" for e in first_call_events)

    # Create a new file
    mock_services.writer.write_events.reset_mock()
    (tmp_path / "new.md").write_text("new file")
    await asyncio.sleep(0.25)  # Wait for poll cycle

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Should have detected the new file
    assert mock_services.writer.write_events.called
    all_events = []
    for call in mock_services.writer.write_events.call_args_list:
        all_events.extend(call[0][1])
    assert any(e.event_type == "fs.file_created" and e.entity_id == "new.md" for e in all_events)


@pytest.mark.asyncio
async def test_run_nonexistent_path(tmp_path, mock_services, source_id):
    """Source should log error and return if path doesn't exist."""
    config = FilesystemSourceConfig(
        type="filesystem",
        path=str(tmp_path / "nonexistent"),
        watch_mode="poll",
        poll_interval=1,
    )
    src = FilesystemSource("test_fs", config, mock_services, source_id)
    await src.run()
    # Should not have written any events
    mock_services.writer.write_events.assert_not_called()


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------

class TestFilesystemConfig:
    def test_minimal_config(self):
        config = FilesystemSourceConfig(type="filesystem", path="/some/path")
        assert config.watch_mode == "hybrid"
        assert config.poll_interval == 300.0  # 5m
        assert config.extensions is None
        assert config.recursive is True
        assert config.include_content_preview is False

    def test_full_config(self):
        config = FilesystemSourceConfig(
            type="filesystem",
            path="/some/path",
            watch_mode="poll",
            poll_interval="30s",
            extensions=[".md", ".org"],
            ignore_patterns=[".git/**", "*.tmp", ".obsidian/**"],
            recursive=True,
            include_content_preview=True,
            content_preview_length=500,
        )
        assert config.poll_interval == 30.0
        assert config.extensions == [".md", ".org"]
        assert ".obsidian/**" in config.ignore_patterns

    def test_invalid_watch_mode(self):
        with pytest.raises(Exception):
            FilesystemSourceConfig(type="filesystem", path="/x", watch_mode="invalid")

    def test_diff_config_defaults(self):
        config = FilesystemSourceConfig(type="filesystem", path="/some/path")
        assert config.max_changed_sections == 5
        assert config.max_section_chars == 300

    def test_diff_config_custom(self):
        config = FilesystemSourceConfig(
            type="filesystem", path="/some/path",
            max_changed_sections=10, max_section_chars=500,
        )
        assert config.max_changed_sections == 10
        assert config.max_section_chars == 500
