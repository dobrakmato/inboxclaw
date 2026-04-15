import asyncio
import hashlib
import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.config import FilesystemSourceConfig
from src.schemas import NewEvent
from src.services import AppServices
from src.utils.text_diff import TextDiffCalculator

logger = logging.getLogger(__name__)


# Extensions that are considered binary (no text diff / preview)
_BINARY_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".mp3", ".mp4", ".wav", ".flac", ".ogg", ".avi", ".mkv", ".mov",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".sqlite", ".db",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
}


def _is_binary(filename: str) -> bool:
    """Return True if the file extension suggests a binary format."""
    _, ext = os.path.splitext(filename)
    return ext.lower() in _BINARY_EXTENSIONS


class FileSnapshot:
    """Immutable snapshot of a file's state at a point in time."""

    __slots__ = ("relative_path", "size_bytes", "modified_time", "content_hash", "content_text")

    def __init__(self, relative_path: str, size_bytes: int, modified_time: float, content_hash: str, content_text: Optional[str] = None):
        self.relative_path = relative_path
        self.size_bytes = size_bytes
        self.modified_time = modified_time
        self.content_hash = content_hash
        self.content_text = content_text


def _hash_file(path: Path, algorithm: str = "sha256") -> str:
    """Compute a hex-digest hash of a file's contents."""
    h = hashlib.new(algorithm)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except OSError:
        return ""
    return f"{algorithm}:{h.hexdigest()}"


def _read_text(path: Path) -> Optional[str]:
    """Read the full text content of a file, or None if binary/unreadable."""
    if _is_binary(path.name):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _content_preview(path: Path, length: int) -> str:
    """Read the first *length* characters of a text file, best-effort."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(length)
    except OSError:
        return ""


def _matches_any(relative: str, patterns: List[str]) -> bool:
    """Return True if *relative* matches any of the glob *patterns*."""
    for pat in patterns:
        if fnmatch(relative, pat):
            return True
        # Also match against each path component for directory patterns
        parts = Path(relative).parts
        for i in range(len(parts)):
            partial = str(Path(*parts[: i + 1]))
            if fnmatch(partial, pat):
                return True
    return False


def _extension_ok(filename: str, extensions: Optional[List[str]]) -> bool:
    """Return True if the file extension is in the allowed set (or if no filter is set)."""
    if not extensions:
        return True
    _, ext = os.path.splitext(filename)
    return ext.lower() in {e.lower() for e in extensions}


class _WatchdogHandler(FileSystemEventHandler):
    """Bridges watchdog events into an asyncio queue."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._queue = queue
        self._loop = loop

    def _enqueue(self, kind: str, src_path: str, dest_path: Optional[str] = None):
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (kind, src_path, dest_path))

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue("created", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._enqueue("modified", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._enqueue("deleted", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._enqueue("moved", event.src_path, event.dest_path)


class FilesystemSource:
    """Generic filesystem watcher source.

    Supports three watch modes:
    - ``watch``: real-time events via watchdog only.
    - ``poll``: periodic full-directory scan only.
    - ``hybrid`` (default): watchdog for real-time + periodic reconciliation scan.
    """

    def __init__(self, name: str, config: FilesystemSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id

        self.path = Path(config.path).resolve()
        self.poll_interval: float = config.poll_interval
        self.watch_mode: str = config.watch_mode
        self.extensions: Optional[List[str]] = config.extensions
        self.ignore_patterns: List[str] = config.ignore_patterns
        self.recursive: bool = config.recursive
        self.include_content_preview: bool = config.include_content_preview
        self.content_preview_length: int = config.content_preview_length

        self.diff_calc = TextDiffCalculator(
            max_section_chars=config.max_section_chars,
            max_changed_sections=config.max_changed_sections,
        )

        # In-memory snapshot cache: relative_path -> FileSnapshot
        self._snapshots: Dict[str, FileSnapshot] = {}
        self._observer: Optional[Observer] = None
        self._watcher_queue: Optional[asyncio.Queue] = None

    # ------------------------------------------------------------------
    # Scanning helpers
    # ------------------------------------------------------------------

    def _relative(self, abs_path: Path) -> str:
        """Return the path relative to the watched root, using forward slashes."""
        try:
            return str(abs_path.resolve().relative_to(self.path)).replace("\\", "/")
        except ValueError:
            return str(abs_path).replace("\\", "/")

    def _should_include(self, relative: str) -> bool:
        """Check extension and ignore-pattern filters."""
        filename = Path(relative).name
        if not _extension_ok(filename, self.extensions):
            return False
        if _matches_any(relative, self.ignore_patterns):
            return False
        return True

    def _take_snapshot(self, abs_path: Path) -> Optional[FileSnapshot]:
        """Build a FileSnapshot for a single file, or None if inaccessible."""
        try:
            stat = abs_path.stat()
        except OSError:
            return None
        relative = self._relative(abs_path)
        content_hash = _hash_file(abs_path)
        content_text = _read_text(abs_path)
        return FileSnapshot(
            relative_path=relative,
            size_bytes=stat.st_size,
            modified_time=stat.st_mtime,
            content_hash=content_hash,
            content_text=content_text,
        )

    def _scan_directory(self) -> Dict[str, FileSnapshot]:
        """Walk the watched directory and return a dict of relative_path -> FileSnapshot."""
        result: Dict[str, FileSnapshot] = {}
        if not self.path.is_dir():
            logger.warning(f"Filesystem source '{self.name}': path does not exist or is not a directory: {self.path}")
            return result

        if self.recursive:
            iterator = self.path.rglob("*")
        else:
            iterator = self.path.glob("*")

        for entry in iterator:
            if not entry.is_file():
                continue
            relative = self._relative(entry)
            if not self._should_include(relative):
                continue
            snap = self._take_snapshot(entry)
            if snap:
                result[relative] = snap
        return result

    # ------------------------------------------------------------------
    # Event building
    # ------------------------------------------------------------------

    def _build_event_data(
        self,
        snapshot: Optional[FileSnapshot],
        previous: Optional[FileSnapshot] = None,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        if snapshot:
            data["path"] = snapshot.relative_path
            data["mime_type"] = mimetypes.guess_type(snapshot.relative_path)[0] or "application/octet-stream"
            data["size_bytes"] = snapshot.size_bytes
            binary = _is_binary(snapshot.relative_path)
            if self.include_content_preview and not binary:
                abs_path = self.path / snapshot.relative_path
                data["content_preview"] = _content_preview(abs_path, self.content_preview_length)
        # Text diff for updated text files
        if previous and snapshot and not _is_binary(snapshot.relative_path):
            if previous.content_text is not None and snapshot.content_text is not None:
                if previous.content_hash != snapshot.content_hash:
                    data["content_diff"] = self.diff_calc.compute_diff(
                        previous.content_text, snapshot.content_text
                    )
        return data

    def _make_event(self, event_type: str, entity_id: str, data: Dict[str, Any], occurred_at: Optional[datetime] = None) -> NewEvent:
        return NewEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            entity_id=entity_id,
            data=data,
            occurred_at=occurred_at or datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Diff logic
    # ------------------------------------------------------------------

    def _diff_snapshots(self, previous: Dict[str, FileSnapshot], current: Dict[str, FileSnapshot]) -> List[NewEvent]:
        """Compare two snapshot dicts and emit events for differences."""
        events: List[NewEvent] = []
        now = datetime.now(timezone.utc)
        prev_keys = set(previous.keys())
        curr_keys = set(current.keys())

        # Deleted files
        for rel in prev_keys - curr_keys:
            data = self._build_event_data(previous[rel])
            events.append(self._make_event("fs.file_deleted", rel, data, now))

        # Created files
        for rel in curr_keys - prev_keys:
            data = self._build_event_data(current[rel])
            events.append(self._make_event("fs.file_created", rel, data, now))

        # Possibly updated files
        for rel in prev_keys & curr_keys:
            old = previous[rel]
            new = current[rel]
            if old.content_hash != new.content_hash or old.size_bytes != new.size_bytes:
                data = self._build_event_data(new, previous=old)
                events.append(self._make_event("fs.file_updated", rel, data, now))

        return events

    def _full_scan_and_diff(self) -> List[NewEvent]:
        """Perform a full directory scan, diff against cached state, and return events."""
        current = self._scan_directory()
        events = self._diff_snapshots(self._snapshots, current)
        self._snapshots = current
        return events

    # ------------------------------------------------------------------
    # Watchdog integration
    # ------------------------------------------------------------------

    def _start_observer(self, loop: asyncio.AbstractEventLoop):
        """Start the watchdog observer in a background thread."""
        self._watcher_queue = asyncio.Queue()
        handler = _WatchdogHandler(self._watcher_queue, loop)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.path), recursive=self.recursive)
        self._observer.daemon = True
        self._observer.start()
        logger.info(f"Filesystem source '{self.name}': watchdog observer started on {self.path}")

    def _stop_observer(self):
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    async def _process_watcher_events(self):
        """Drain the watcher queue and emit events."""
        if not self._watcher_queue:
            return

        batch: List[tuple] = []
        while not self._watcher_queue.empty():
            try:
                batch.append(self._watcher_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            return

        events: List[NewEvent] = []
        now = datetime.now(timezone.utc)

        for item in batch:
            kind, src_path_str = item[0], item[1]
            dest_path_str = item[2] if len(item) > 2 else None

            if kind == "moved" and dest_path_str:
                src_abs = Path(src_path_str).resolve()
                dest_abs = Path(dest_path_str).resolve()
                src_rel = self._relative(src_abs)
                dest_rel = self._relative(dest_abs)

                # Remove old from cache
                if not self._should_include(dest_rel):
                    # Moved to an ignored location — treat as delete
                    if src_rel in self._snapshots:
                        data = self._build_event_data(self._snapshots[src_rel])
                        events.append(self._make_event("fs.file_deleted", src_rel, data, now))
                        del self._snapshots[src_rel]
                    continue

                snap = self._take_snapshot(dest_abs)
                if snap:
                    old_snap = self._snapshots.pop(src_rel, None)
                    self._snapshots[dest_rel] = snap
                    data = self._build_event_data(snap)
                    data["old_path"] = src_rel
                    events.append(self._make_event("fs.file_moved", dest_rel, data, now))
                continue

            abs_path = Path(src_path_str).resolve()
            relative = self._relative(abs_path)

            if not self._should_include(relative):
                continue

            if kind == "created":
                snap = self._take_snapshot(abs_path)
                if snap:
                    self._snapshots[relative] = snap
                    data = self._build_event_data(snap)
                    events.append(self._make_event("fs.file_created", relative, data, now))

            elif kind == "modified":
                snap = self._take_snapshot(abs_path)
                if snap:
                    old = self._snapshots.get(relative)
                    if old and old.content_hash == snap.content_hash:
                        continue  # No real change
                    self._snapshots[relative] = snap
                    data = self._build_event_data(snap, previous=old)
                    events.append(self._make_event("fs.file_updated", relative, data, now))

            elif kind == "deleted":
                old = self._snapshots.pop(relative, None)
                data = self._build_event_data(old) if old else {"path": relative}
                events.append(self._make_event("fs.file_deleted", relative, data, now))

        if events:
            self.services.writer.write_events(self.source_id, events)

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self):
        logger.info(
            f"Starting Filesystem source '{self.name}' "
            f"(path={self.path}, mode={self.watch_mode}, poll_interval={self.poll_interval}s)"
        )

        if not self.path.is_dir():
            logger.error(f"Filesystem source '{self.name}': path does not exist: {self.path}")
            return

        # Initial full scan to establish baseline and catch anything missed while offline
        initial_events = self._full_scan_and_diff()
        if initial_events:
            self.services.writer.write_events(self.source_id, initial_events)
            logger.info(f"Filesystem source '{self.name}': initial scan found {len(initial_events)} changes")

        # Start watchdog if needed
        use_watcher = self.watch_mode in ("watch", "hybrid")
        use_poll = self.watch_mode in ("poll", "hybrid")

        if use_watcher:
            loop = asyncio.get_running_loop()
            self._start_observer(loop)

        try:
            if use_watcher and not use_poll:
                # Watch-only mode: just drain the queue periodically
                while True:
                    await asyncio.sleep(0.5)
                    await self._process_watcher_events()
            elif use_poll and not use_watcher:
                # Poll-only mode
                while True:
                    await asyncio.sleep(self.poll_interval)
                    events = self._full_scan_and_diff()
                    if events:
                        self.services.writer.write_events(self.source_id, events)
            else:
                # Hybrid mode: drain watcher frequently, reconcile periodically
                elapsed = 0.0
                interval = 0.5
                while True:
                    await asyncio.sleep(interval)
                    elapsed += interval
                    await self._process_watcher_events()
                    if elapsed >= self.poll_interval:
                        elapsed = 0.0
                        events = self._full_scan_and_diff()
                        if events:
                            self.services.writer.write_events(self.source_id, events)
        except asyncio.CancelledError:
            pass
        finally:
            self._stop_observer()
            logger.info(f"Filesystem source '{self.name}' stopped.")
