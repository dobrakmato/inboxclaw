from datetime import datetime, timedelta, timezone

from src.utils.google_drive_sync import (
    DriveDebounceManager,
    DriveDebounceState,
    DriveFileSnapshot,
    DriveTransitionClassifier,
    GoogleDriveEventType,
    DriveTextDiffCalculator,
)


def make_snapshot(**overrides):
    base = {
        "file_id": "f1",
        "name": "Doc",
        "mime_type": "text/plain",
        "parents": ["p1"],
        "trashed": False,
        "created_time": "2026-03-14T09:00:00Z",
        "modified_time": "2026-03-14T10:00:00Z",
        "version": "1",
        "owned_by_me": True,
        "shared_with_me_time": None,
        "sharing_user": None,
        "description": None,
        "indexable_text": None,
        "last_modifying_user": None,
    }
    base.update(overrides)
    return DriveFileSnapshot(**base)


def test_classifier_created_removed_and_move():
    classifier = DriveTransitionClassifier()

    created = classifier.classify(None, make_snapshot(), removed=False)
    assert created == [GoogleDriveEventType.FILE_CREATED]

    removed = classifier.classify(make_snapshot(), None, removed=True)
    assert removed == [GoogleDriveEventType.FILE_REMOVED]

    moved = classifier.classify(
        make_snapshot(parents=["a"]),
        make_snapshot(parents=["b"], version="2"),
        removed=False,
    )
    assert GoogleDriveEventType.FILE_MOVED in moved


def test_classifier_share_changed():
    classifier = DriveTransitionClassifier()
    previous = make_snapshot(owned_by_me=False, shared_with_me_time=None)
    current = make_snapshot(
        owned_by_me=False,
        shared_with_me_time="2026-03-14T11:00:00Z",
        sharing_user={"displayName": "Alice", "emailAddress": "alice@example.com"},
        version="2",
    )

    event_types = classifier.classify(previous, current, removed=False)

    assert GoogleDriveEventType.FILE_SHARED_WITH_YOU in event_types


def test_debounce_flush_by_quiet_window():
    manager = DriveDebounceManager()
    now = datetime.now(timezone.utc)
    state = manager.mark_dirty(
        None,
        now=now,
        start_version="1",
        latest_version="4",
    )
    assert isinstance(state, DriveDebounceState)
    assert state.raw_change_count == 1
    assert state.start_version == "1"
    assert state.latest_version == "4"

    should_not_flush = manager.should_flush(
        state,
        now=now + timedelta(seconds=30),
        quiet_window_seconds=60,
        max_session_seconds=600,
    )
    assert not should_not_flush

    should_flush = manager.should_flush(
        state,
        now=now + timedelta(seconds=61),
        quiet_window_seconds=60,
        max_session_seconds=600,
    )
    assert should_flush


def test_text_diff_calculator():
    calc = DriveTextDiffCalculator(max_snippet_chars=10)
    
    old_text = "Paragraph 1\n\nParagraph 2\n\nParagraph 3"
    new_text = "Paragraph 1\n\nParagraph 2 modified\n\nParagraph 4"
    
    diff = calc.compute_diff(old_text, new_text)
    
    assert diff["changedBlockCount"] == 2
    assert diff["snippetBefore"] == "Paragraph ... (truncated)"
    assert diff["snippetAfter"] == "Paragraph ... (truncated)"
    assert diff["addedCharCount"] > 0
    assert diff["removedCharCount"] > 0


def test_text_diff_calculator_empty():
    calc = DriveTextDiffCalculator()
    diff = calc.compute_diff(None, "Hello")
    assert diff["changedBlockCount"] == 1
    assert diff["snippetBefore"] is None
    assert diff["snippetAfter"] == "Hello"
