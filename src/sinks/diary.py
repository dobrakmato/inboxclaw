import asyncio
import calendar
import json
import logging
import os
import tempfile
import time as time_module
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Union

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from src.config import DiarySinkConfig
from src.database import Event
from src.pipeline.matcher import EventMatcher
from src.schemas import EventWithMeta
from src.services import AppServices

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is available on supported Python versions.
    ZoneInfo = None

logger = logging.getLogger(__name__)

DIARY_DIRECTORIES = ("raw", "user", "daily", "weekly", "monthly", ".diary")
TEMP_PREFIX = ".diary-tmp-"
RECONCILE_INTERVAL_SECONDS = 60.0
DAILY_SKIPPED_MARKER_TEMPLATE = (
    "<daily summary for {period} was skipped because user notes and raw events were both missing>"
)
WEEKLY_SKIPPED_MARKER_TEMPLATE = (
    "<weekly summary for {period} was skipped because all daily summaries were missing or skipped>"
)
MONTHLY_SKIPPED_MARKER_TEMPLATE = (
    "<monthly summary for {period} was skipped because all daily summaries were missing or skipped>"
)


@dataclass(frozen=True)
class DiaryConfig:
    root: Path
    cutoff_time: time
    timezone: tzinfo
    lock_timeout: float
    max_backfill_days: int

    @classmethod
    def from_sink_config(cls, config: DiarySinkConfig) -> "DiaryConfig":
        return cls(
            root=Path(config.path),
            cutoff_time=parse_cutoff_time(config.cutoff_time),
            timezone=resolve_timezone(config.timezone),
            lock_timeout=config.lock_timeout,
            max_backfill_days=config.max_backfill_days,
        )


def parse_cutoff_time(value: str) -> time:
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("Diary cutoff_time must use HH:MM or HH:MM:SS format")

    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour=hour, minute=minute, second=second)
    except ValueError as exc:
        raise ValueError("Diary cutoff_time must use HH:MM or HH:MM:SS format") from exc


def resolve_timezone(value: Optional[str]) -> tzinfo:
    if value is None or value == "":
        return timezone.utc

    if value.lower() == "local":
        raise ValueError("Diary timezone 'local' is not supported; use UTC or an IANA timezone name")

    if value.upper() == "UTC":
        return timezone.utc

    if ZoneInfo is None:
        raise ValueError(f"Named diary timezone is not supported: {value!r}")

    try:
        return ZoneInfo(value)
    except Exception as exc:
        raise ValueError(f"Invalid diary timezone: {value!r}") from exc


def is_windows_admin() -> bool:
    if os.name != "nt":
        return True

    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def resolve_diary_date(moment: datetime, cutoff: time, diary_tz: tzinfo) -> date:
    local_moment = _coerce_to_timezone(moment, diary_tz)
    cutoff_moment = datetime.combine(local_moment.date(), cutoff, tzinfo=diary_tz)
    if local_moment < cutoff_moment:
        return local_moment.date() - timedelta(days=1)
    return local_moment.date()


def _coerce_to_timezone(moment: datetime, diary_tz: tzinfo) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=diary_tz)
    return moment.astimezone(diary_tz)


def _date_path(config: DiaryConfig, directory: str, day: date) -> Path:
    return config.root / directory / f"{day.isoformat()}.md"


def _raw_path(config: DiaryConfig, day: date) -> Path:
    return _date_path(config, "raw", day)


def _user_path(config: DiaryConfig, day: date) -> Path:
    return _date_path(config, "user", day)


def _daily_path(config: DiaryConfig, day: date) -> Path:
    return _date_path(config, "daily", day)


def _weekly_path(config: DiaryConfig, week_id: str) -> Path:
    return config.root / "weekly" / f"{week_id}.md"


def _monthly_path(config: DiaryConfig, month_id: str) -> Path:
    return config.root / "monthly" / f"{month_id}.md"


def _iso_week_id(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _latest_completed_week_start(closed_day: date) -> date:
    days_since_sunday = (closed_day.weekday() + 1) % 7
    latest_sunday = closed_day - timedelta(days=days_since_sunday)
    return latest_sunday - timedelta(days=6)


def _month_id(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_tuple(month_id: str) -> tuple[int, int]:
    year_text, month_text = month_id.split("-", 1)
    return int(year_text), int(month_text)


def _previous_month_id(month_id: str) -> str:
    year, month = _month_tuple(month_id)
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def _next_month_id(month_id: str) -> str:
    year, month = _month_tuple(month_id)
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def _last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _latest_completed_month_id(closed_day: date) -> str:
    month_end = _last_day_of_month(closed_day.year, closed_day.month)
    if closed_day >= month_end:
        return _month_id(closed_day)
    return _previous_month_id(_month_id(closed_day))


def _days_in_month(month_id: str) -> Iterable[date]:
    year, month = _month_tuple(month_id)
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        yield date(year, month, day)


def _day_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _parse_date_stem(path: Path) -> Optional[date]:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def _collect_date_files(directory: Path, through: date) -> set[date]:
    if not directory.exists():
        return set()

    dates: set[date] = set()
    for path in directory.glob("*.md"):
        parsed = _parse_date_stem(path)
        if parsed is not None and parsed <= through:
            dates.add(parsed)
    return dates


def _collect_date_files_between(directory: Path, start: date, through: date) -> set[date]:
    return {day for day in _collect_date_files(directory, through) if day >= start}


def _ensure_directory(path: Path, sink_name: str) -> None:
    if path.exists():
        return

    path.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Diary sink '%s' directory created: %s",
        sink_name,
        path,
        extra={"operation": "directory_created", "path": str(path)},
    )


def ensure_diary_structure(config: DiaryConfig, sink_name: str) -> None:
    _ensure_directory(config.root, sink_name)
    for directory in DIARY_DIRECTORIES:
        _ensure_directory(config.root / directory, sink_name)


def ensure_current_user_note(config: DiaryConfig, current_diary_date: date, sink_name: str) -> None:
    path = _user_path(config, current_diary_date)
    if path.exists():
        return

    _ensure_directory(path.parent, sink_name)
    with open(path, "x", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)
    logger.info(
        "Diary sink '%s' empty user note created: %s",
        sink_name,
        path,
        extra={
            "operation": "empty_user_note_created",
            "period_type": "daily",
            "period": current_diary_date.isoformat(),
            "path": str(path),
        },
    )


def _fsync_directory(path: Path) -> None:
    try:
        flags = getattr(os, "O_RDONLY", 0)
        fd = os.open(path, flags)
    except OSError:
        return

    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, content: str) -> None:
    _ensure_directory(path.parent, "")
    temp_path: Optional[Path] = None
    fd = -1
    try:
        fd, temp_name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        if fd != -1:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        raise


def atomic_replace_symlink(link_path: Path, target_path: Path) -> bool:
    desired_target = os.path.relpath(target_path, start=link_path.parent)
    if _symlink_matches(link_path, target_path, desired_target):
        return False

    _ensure_directory(link_path.parent, "")
    temp_link = link_path.with_name(f"{TEMP_PREFIX}{link_path.name}-{uuid.uuid4().hex}")
    try:
        os.symlink(desired_target, temp_link, target_is_directory=False)
        os.replace(temp_link, link_path)
        _fsync_directory(link_path.parent)
        return True
    except Exception:
        try:
            if temp_link.exists() or temp_link.is_symlink():
                temp_link.unlink()
        except FileNotFoundError:
            pass
        raise


def _symlink_matches(link_path: Path, target_path: Path, desired_target: str) -> bool:
    if not link_path.is_symlink():
        return False

    current_target = os.readlink(link_path)
    if current_target == desired_target:
        return True

    current_path = Path(current_target)
    if not current_path.is_absolute():
        current_path = link_path.parent / current_path

    return current_path.resolve(strict=False) == target_path.resolve(strict=False)


def render_input_file(path: Path, label: str, period: str) -> str:
    if not path.exists():
        return f"<{label} for {period} was missing>"

    content = path.read_text(encoding="utf-8")
    if content == "":
        return f"<{label} for {period} was present but empty>"
    return content.rstrip("\n")


def render_period_artifact(title: str, sections: list[tuple[str, str]]) -> str:
    output: list[str] = [f"# {title}", ""]
    for heading, body in sections:
        output.extend([f"## {heading}", "", body, ""])
    return "\n".join(output).rstrip() + "\n"


def merge_daily(config: DiaryConfig, day: date) -> str:
    previous_day = day - timedelta(days=1)
    return render_period_artifact(
        f"Daily summary for {day.isoformat()}",
        [
            (
                f"Previous daily summary ({previous_day.isoformat()})",
                render_input_file(_daily_path(config, previous_day), "daily summary", previous_day.isoformat()),
            ),
            (
                f"User notes ({day.isoformat()})",
                render_input_file(_user_path(config, day), "user notes file", day.isoformat()),
            ),
            (
                f"Raw events ({day.isoformat()})",
                render_input_file(_raw_path(config, day), "raw events file", day.isoformat()),
            ),
        ],
    )


def merge_weekly(config: DiaryConfig, week_start: date) -> str:
    week_id = _iso_week_id(week_start)
    previous_week_id = _iso_week_id(week_start - timedelta(days=7))
    sections = [
        (
            f"Previous weekly summary ({previous_week_id})",
            render_input_file(_weekly_path(config, previous_week_id), "weekly summary", previous_week_id),
        )
    ]

    for day in _day_range(week_start, week_start + timedelta(days=6)):
        sections.append(
            (
                f"Daily summary ({day.isoformat()})",
                render_input_file(_daily_path(config, day), "daily summary", day.isoformat()),
            )
        )

    return render_period_artifact(f"Weekly summary for {week_id}", sections)


def merge_monthly(config: DiaryConfig, month_id: str) -> str:
    previous_month = _previous_month_id(month_id)
    sections = [
        (
            f"Previous monthly summary ({previous_month})",
            render_input_file(_monthly_path(config, previous_month), "monthly summary", previous_month),
        )
    ]

    for day in _days_in_month(month_id):
        sections.append(
            (
                f"Daily summary ({day.isoformat()})",
                render_input_file(_daily_path(config, day), "daily summary", day.isoformat()),
            )
        )

    return render_period_artifact(f"Monthly summary for {month_id}", sections)


def generate_or_skip_daily(config: DiaryConfig, day: date, sink_name: str) -> None:
    output_path = _daily_path(config, day)
    if output_path.exists():
        return

    user_path = _user_path(config, day)
    raw_path = _raw_path(config, day)
    if not user_path.exists() and not raw_path.exists():
        content = _daily_skipped_marker(day) + "\n"
        atomic_write_text(output_path, content)
        logger.info(
            "Diary sink '%s' daily summary skipped for %s",
            sink_name,
            day.isoformat(),
            extra={
                "operation": "daily_summary_skipped",
                "period_type": "daily",
                "period": day.isoformat(),
                "reason": "user notes and raw events were both missing",
                "output_path": str(output_path),
            },
        )
        return

    atomic_write_text(output_path, merge_daily(config, day))
    logger.info(
        "Diary sink '%s' daily summary generated for %s",
        sink_name,
        day.isoformat(),
        extra={
            "operation": "daily_summary_generated",
            "period_type": "daily",
            "period": day.isoformat(),
            "output_path": str(output_path),
        },
    )


def _daily_skipped_marker(day: date) -> str:
    return DAILY_SKIPPED_MARKER_TEMPLATE.format(period=day.isoformat())


def _weekly_skipped_marker(week_id: str) -> str:
    return WEEKLY_SKIPPED_MARKER_TEMPLATE.format(period=week_id)


def _monthly_skipped_marker(month_id: str) -> str:
    return MONTHLY_SKIPPED_MARKER_TEMPLATE.format(period=month_id)


def _artifact_matches_marker(path: Path, marker: str) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip() == marker
    except OSError:
        return False


def _daily_artifact_is_meaningful(config: DiaryConfig, day: date) -> bool:
    path = _daily_path(config, day)
    return path.exists() and not _artifact_matches_marker(path, _daily_skipped_marker(day))


def generate_or_skip_weekly(config: DiaryConfig, week_start: date, sink_name: str) -> None:
    week_id = _iso_week_id(week_start)
    output_path = _weekly_path(config, week_id)
    if output_path.exists():
        return

    if not any(_daily_artifact_is_meaningful(config, day) for day in _day_range(week_start, week_start + timedelta(days=6))):
        atomic_write_text(output_path, _weekly_skipped_marker(week_id) + "\n")
        logger.info(
            "Diary sink '%s' weekly summary skipped for %s",
            sink_name,
            week_id,
            extra={
                "operation": "weekly_summary_skipped",
                "period_type": "weekly",
                "period": week_id,
                "reason": "all daily summaries were missing or skipped",
                "output_path": str(output_path),
            },
        )
        return

    atomic_write_text(output_path, merge_weekly(config, week_start))
    logger.info(
        "Diary sink '%s' weekly summary generated for %s",
        sink_name,
        week_id,
        extra={
            "operation": "weekly_summary_generated",
            "period_type": "weekly",
            "period": week_id,
            "output_path": str(output_path),
        },
    )


def generate_or_skip_monthly(config: DiaryConfig, month_id: str, sink_name: str) -> None:
    output_path = _monthly_path(config, month_id)
    if output_path.exists():
        return

    if not any(_daily_artifact_is_meaningful(config, day) for day in _days_in_month(month_id)):
        atomic_write_text(output_path, _monthly_skipped_marker(month_id) + "\n")
        logger.info(
            "Diary sink '%s' monthly summary skipped for %s",
            sink_name,
            month_id,
            extra={
                "operation": "monthly_summary_skipped",
                "period_type": "monthly",
                "period": month_id,
                "reason": "all daily summaries were missing or skipped",
                "output_path": str(output_path),
            },
        )
        return

    atomic_write_text(output_path, merge_monthly(config, month_id))
    logger.info(
        "Diary sink '%s' monthly summary generated for %s",
        sink_name,
        month_id,
        extra={
            "operation": "monthly_summary_generated",
            "period_type": "monthly",
            "period": month_id,
            "output_path": str(output_path),
        },
    )


def write_raw_entry(config: DiaryConfig, event: Event, line: str, sink_name: str) -> None:
    created_at = event.created_at
    if created_at is None:
        created_at = datetime.now(timezone.utc)

    diary_day = resolve_diary_date(created_at, config.cutoff_time, config.timezone)
    output_path = _raw_path(config, diary_day)
    _ensure_directory(output_path.parent, sink_name)
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(output_path.parent)
    logger.info(
        "Diary sink '%s' raw entry written for %s",
        sink_name,
        diary_day.isoformat(),
        extra={
            "operation": "raw_entry_written",
            "period_type": "daily",
            "period": diary_day.isoformat(),
            "path": str(output_path),
        },
    )


def reconcile_diary(config: DiaryConfig, sink_name: str, now: Optional[datetime] = None) -> None:
    with DiaryLock(config, sink_name):
        ensure_diary_structure(config, sink_name)
        _cleanup_temporary_files(config, sink_name)

        current_time = now or datetime.now(config.timezone)
        current_diary_date = resolve_diary_date(current_time, config.cutoff_time, config.timezone)
        closed_day = current_diary_date - timedelta(days=1)

        ensure_current_user_note(config, current_diary_date, sink_name)
        _update_symlink(
            config,
            "today.md",
            _user_path(config, current_diary_date),
            sink_name,
            "daily",
            current_diary_date.isoformat(),
        )

        reconcile_start = _reconciliation_start_date(config, closed_day)
        for day in _day_range(reconcile_start, closed_day):
            generate_or_skip_daily(config, day, sink_name)

        for week_start in _weekly_starts_for_window(reconcile_start, closed_day):
            generate_or_skip_weekly(config, week_start, sink_name)

        for month_id in _month_ids_for_window(reconcile_start, closed_day):
            generate_or_skip_monthly(config, month_id, sink_name)

        _update_symlink(
            config,
            "yesterday.md",
            _daily_path(config, closed_day),
            sink_name,
            "daily",
            closed_day.isoformat(),
        )
        latest_week_start = _latest_completed_week_start(closed_day)
        _update_symlink_if_target_exists(
            config,
            "last_week.md",
            _weekly_path(config, _iso_week_id(latest_week_start)),
            sink_name,
            "weekly",
            _iso_week_id(latest_week_start),
        )
        latest_month = _latest_completed_month_id(closed_day)
        _update_symlink_if_target_exists(
            config,
            "last_month.md",
            _monthly_path(config, latest_month),
            sink_name,
            "monthly",
            latest_month,
        )


def _reconciliation_start_date(config: DiaryConfig, closed_day: date) -> date:
    max_backfill_days = max(config.max_backfill_days, 1)
    earliest_allowed_day = closed_day - timedelta(days=max_backfill_days - 1)

    source_dates = (
        _collect_date_files_between(config.root / "raw", earliest_allowed_day, closed_day)
        | _collect_date_files_between(config.root / "user", earliest_allowed_day, closed_day)
    )
    generated_dates = _collect_date_files_between(config.root / "daily", earliest_allowed_day, closed_day)
    relevant_dates = source_dates | generated_dates
    start = min(relevant_dates) if relevant_dates else closed_day

    return max(start, earliest_allowed_day)


def _weekly_starts_for_window(daily_start: date, closed_day: date) -> Iterable[date]:
    if daily_start > closed_day:
        return []

    latest_week_start = _latest_completed_week_start(closed_day)
    week_start = _week_start(daily_start)
    starts: list[date] = []
    while week_start <= latest_week_start:
        week_end = week_start + timedelta(days=6)
        if week_end >= daily_start and week_start <= closed_day:
            starts.append(week_start)
        week_start += timedelta(days=7)
    return starts


def _month_ids_for_window(daily_start: date, closed_day: date) -> Iterable[str]:
    if daily_start > closed_day:
        return []

    latest_month = _latest_completed_month_id(closed_day)
    month_id = _month_id(daily_start)
    month_ids: list[str] = []
    while _month_tuple(month_id) <= _month_tuple(latest_month):
        year, month = _month_tuple(month_id)
        month_start = date(year, month, 1)
        month_end = _last_day_of_month(year, month)
        if month_end >= daily_start and month_start <= closed_day:
            month_ids.append(month_id)
        month_id = _next_month_id(month_id)
    return month_ids


def _update_symlink(
    config: DiaryConfig,
    link_name: str,
    target_path: Path,
    sink_name: str,
    period_type: str,
    period: str,
) -> None:
    link_path = config.root / link_name
    updated = atomic_replace_symlink(link_path, target_path)
    if not updated:
        return

    logger.info(
        "Diary sink '%s' symlink updated: %s -> %s",
        sink_name,
        link_path,
        target_path,
        extra={
            "operation": "symlink_updated",
            "period_type": period_type,
            "period": period,
            "path": str(link_path),
            "target": str(target_path),
        },
    )


def _update_symlink_if_target_exists(
    config: DiaryConfig,
    link_name: str,
    target_path: Path,
    sink_name: str,
    period_type: str,
    period: str,
) -> None:
    if not target_path.exists():
        return

    _update_symlink(config, link_name, target_path, sink_name, period_type, period)


def _cleanup_temporary_files(config: DiaryConfig, sink_name: str) -> None:
    if not config.root.exists():
        return

    for path in config.root.rglob(f"{TEMP_PREFIX}*"):
        if path.name == "lock":
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                continue
            path.unlink()
            logger.info(
                "Diary sink '%s' recovery action performed: removed stale temporary file %s",
                sink_name,
                path,
                extra={"operation": "recovery_action_performed", "path": str(path)},
            )
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning(
                "Diary sink '%s' failed to remove stale temporary file %s",
                sink_name,
                path,
                exc_info=True,
                extra={"operation": "recovery_action_failed", "path": str(path)},
            )


class DiaryLock:
    def __init__(self, config: DiaryConfig, sink_name: str):
        self.config = config
        self.sink_name = sink_name
        self.lock_path = config.root / ".diary" / "lock"
        self._file: Optional[Any] = None
        self._acquired = False

    def __enter__(self) -> "DiaryLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()

    def acquire(self) -> None:
        _ensure_directory(self.config.root, self.sink_name)
        _ensure_directory(self.lock_path.parent, self.sink_name)
        start = time_module.monotonic()

        while True:
            self.lock_path.touch(exist_ok=True)
            self._file = open(self.lock_path, "r+", encoding="utf-8")
            if self._try_lock():
                self._acquired = True
                logger.info(
                    "Diary sink '%s' lock acquired",
                    self.sink_name,
                    extra={"operation": "lock_acquired", "path": str(self.lock_path)},
                )
                return

            self._close_file()
            elapsed = time_module.monotonic() - start
            if elapsed >= self.config.lock_timeout:
                raise TimeoutError(f"Timed out acquiring diary lock: {self.lock_path}")

            time_module.sleep(min(0.25, self.config.lock_timeout - elapsed))

    def release(self) -> None:
        if not self._acquired:
            self._close_file()
            return

        try:
            self._unlock()
        finally:
            self._acquired = False
            self._close_file()
            logger.info(
                "Diary sink '%s' lock released",
                self.sink_name,
                extra={"operation": "lock_released", "path": str(self.lock_path)},
            )

    def _try_lock(self) -> bool:
        assert self._file is not None
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(self) -> None:
        assert self._file is not None
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)

    def _close_file(self) -> None:
        if self._file is None:
            return
        self._file.close()
        self._file = None


class DiarySink:
    def __init__(
        self,
        name: str,
        config: Union[DiarySinkConfig, Dict[str, Any]],
        services: AppServices,
    ):
        if isinstance(config, dict):
            try:
                config = DiarySinkConfig(**config)
            except ValidationError as exc:
                for error in exc.errors():
                    if error["type"] == "missing":
                        raise KeyError(f"'{error['loc'][0]}'")
                raise exc

        self.name = name
        self.services = services
        self.config = config
        self.diary_config = DiaryConfig.from_sink_config(config)
        self.matcher = EventMatcher(config.match)
        self._task: asyncio.Task | None = None
        self._last_event_id = self._get_last_event_id()

    @property
    def match(self) -> Any:
        if len(self.matcher.patterns) == 1:
            return self.matcher.patterns[0]
        return self.matcher.patterns

    @match.setter
    def match(self, value: Any):
        self.matcher = EventMatcher(value)

    async def start(self) -> None:
        if self._task is not None:
            return

        if os.name == "nt" and not is_windows_admin():
            logger.warning(
                "Diary sink '%s' disabled: Windows administrator privileges are required "
                "to create diary convenience symlinks",
                self.name,
                extra={"operation": "diary_sink_disabled", "reason": "windows_requires_admin_symlink"},
            )
            return

        self.reconcile()
        self._task = self.services.add_task(self._run_loop())
        logger.info("Diary sink '%s' started (path=%s)", self.name, self.config.path)

    async def stop(self) -> None:
        if self._task is None:
            return

        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("Diary sink '%s' stopped", self.name)

    def reconcile(self, now: Optional[datetime] = None) -> None:
        reconcile_diary(self.diary_config, self.name, now=now)

    async def _run_loop(self) -> None:
        notification_event = None
        try:
            notification_event = self.services.notifier.subscribe()
            while True:
                try:
                    await asyncio.wait_for(notification_event.wait(), timeout=RECONCILE_INTERVAL_SECONDS)
                    notification_event.clear()
                except asyncio.TimeoutError:
                    pass
                self._last_event_id = self.process_new_events(self._last_event_id)
                self.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in diary sink '%s' loop", self.name)
        finally:
            if notification_event is not None:
                self.services.notifier.unsubscribe(notification_event)

    def process_new_events(self, last_id: int) -> int:
        events = self._load_new_events(last_id)
        if not events:
            return last_id

        for event in events:
            if self.matcher.matches(event.event_type):
                self._append_event(event)

        return events[-1].id

    def _load_new_events(self, last_id: int) -> list[Event]:
        with self.services.db_session_maker() as session:
            stmt = (
                select(Event)
                .options(joinedload(Event.source))
                .where(Event.id > last_id)
                .order_by(Event.id.asc())
            )
            return list(session.scalars(stmt).all())

    def _append_event(self, event: Event) -> None:
        dto = EventWithMeta.from_event(event)
        line = json.dumps(dto.to_dict(), ensure_ascii=False, default=str)

        try:
            with DiaryLock(self.diary_config, self.name):
                write_raw_entry(self.diary_config, event, line, self.name)
        except Exception:
            logger.exception(
                "Diary sink '%s' failed to write event %s",
                self.name,
                event.event_id,
            )

    def _get_last_event_id(self) -> int:
        with self.services.db_session_maker() as session:
            try:
                stmt = select(func.max(Event.id))
                return session.scalar(stmt) or 0
            except Exception:
                return 0
