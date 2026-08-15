"""Source-driven health reporting for Inboxclaw."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

from src.schemas import NewEvent

if TYPE_CHECKING:
    from src.services import AppServices


logger = logging.getLogger("inboxclaw.health")

HEALTH_NOTIFICATION_KEY = "_health:last_notified_status"
TRANSIENT_FAILURE_THRESHOLD = 2
IMMEDIATE_UNHEALTHY_CODES = frozenset(
    {
        "authentication",
        "authorization",
        "backoff",
        "configuration",
        "expired",
        "initialization",
        "not_reporting",
        "rate_limited",
        "runner_stopped",
        "stale",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error_status(error: BaseException) -> Optional[int]:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        response = getattr(error, "resp", None)
        status = getattr(response, "status", None)
    return status if isinstance(status, int) else None


def classify_health_error(error: BaseException) -> tuple[str, str]:
    """Return a safe, stable health code and message for an exception."""
    status = _error_status(error)
    if status == 401:
        return "authentication", "Authentication was rejected by the upstream service."
    if status == 403:
        return "authorization", "The configured identity does not have the required access."
    if status == 408:
        return "timeout", "The upstream service timed out."
    if status == 409:
        return "rate_limited", "The upstream service rejected the request because of its rate limit."
    if status == 429:
        return "rate_limited", "The upstream service rate limit was reached."
    if status is not None and status >= 500:
        return "upstream", f"The upstream service returned HTTP {status}."

    if isinstance(error, (FileNotFoundError, ValueError)):
        return "configuration", "The source configuration or local credentials are invalid."
    if isinstance(error, PermissionError):
        return "authorization", "Inboxclaw cannot access a required local resource."
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout", "The source operation timed out."
    if isinstance(error, OSError):
        return "connectivity", "Inboxclaw could not connect to a required resource."
    return "internal", "The source operation failed unexpectedly."


@dataclass
class SourceHealthState:
    name: str
    source_type: str
    source_id: int
    expected_interval: Optional[float]
    status: str = "starting"
    code: Optional[str] = None
    message: str = "Awaiting the first completed source operation."
    action: Optional[str] = None
    registered_at: Optional[datetime] = None
    checked_at: Optional[datetime] = None
    last_healthy_at: Optional[datetime] = None
    last_started_at: Optional[datetime] = None
    task: Optional[asyncio.Task] = None
    last_notified_status: Optional[str] = None
    consecutive_failures: int = 0
    pending_code: Optional[str] = None
    pending_message: Optional[str] = None
    pending_action: Optional[str] = None
    pending_failure_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.registered_at is None:
            self.registered_at = _utcnow()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.source_type,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "action": self.action,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "last_healthy_at": self.last_healthy_at.isoformat() if self.last_healthy_at else None,
            "pending_failure": (
                {
                    "code": self.pending_code,
                    "message": self.pending_message,
                    "action": self.pending_action,
                    "observed_at": (
                        self.pending_failure_at.isoformat()
                        if self.pending_failure_at
                        else None
                    ),
                    "consecutive_failures": self.consecutive_failures,
                    "required_failures": TRANSIENT_FAILURE_THRESHOLD,
                }
                if self.consecutive_failures
                else None
            ),
        }


class SourceHealthReporter:
    """Small source-facing API for reporting actual operational outcomes."""

    def __init__(self, registry: "SourceHealthRegistry", source_name: str):
        self._registry = registry
        self._source_name = source_name

    def checking(self) -> None:
        self._registry.checking(self._source_name)

    def healthy(self, message: str = "The latest source operation completed successfully.") -> None:
        self._registry.healthy(self._source_name, message)

    def unhealthy(
        self,
        code: str,
        message: str,
        *,
        action: Optional[str] = None,
    ) -> None:
        self._registry.unhealthy(self._source_name, code, message, action=action)

    def unhealthy_from_exception(self, error: BaseException) -> None:
        code, message = classify_health_error(error)
        self.unhealthy(code, message)


class SourceHealthRegistry:
    """In-memory source health registry with transition-only system events."""

    def __init__(self, services: "AppServices"):
        self.services = services
        self.sources: dict[str, SourceHealthState] = {}
        self.internal_source_id: Optional[int] = None
        self._stopping = False

    def set_internal_source(self, source_id: int) -> None:
        self.internal_source_id = source_id

    def register(
        self,
        name: str,
        source_type: str,
        source_id: int,
        *,
        expected_interval: Optional[float],
    ) -> SourceHealthReporter:
        notified = self.services.kv.get(source_id, HEALTH_NOTIFICATION_KEY)
        self.sources[name] = SourceHealthState(
            name=name,
            source_type=source_type,
            source_id=source_id,
            expected_interval=expected_interval,
            last_notified_status=notified if notified in {"healthy", "unhealthy"} else None,
        )
        return SourceHealthReporter(self, name)

    def reporter(self, name: str) -> SourceHealthReporter:
        if name not in self.sources:
            raise KeyError(f"Source health reporter is not registered: {name}")
        return SourceHealthReporter(self, name)

    def attach_task(self, name: str, task: asyncio.Task) -> None:
        state = self.sources[name]
        state.task = task

        def source_task_done(done_task: asyncio.Task) -> None:
            if self._stopping or done_task.cancelled():
                return
            try:
                error = done_task.exception()
            except asyncio.CancelledError:
                return
            if state.status == "unhealthy":
                return
            message = "The source background task stopped unexpectedly."
            if error is not None:
                _, error_message = classify_health_error(error)
                message = f"The source background task stopped after an error. {error_message}"
            self.unhealthy(name, "runner_stopped", message)

        task.add_done_callback(source_task_done)

    def checking(self, name: str) -> None:
        self.sources[name].last_started_at = _utcnow()

    def healthy(self, name: str, message: str) -> None:
        now = _utcnow()
        state = self.sources[name]
        state.status = "healthy"
        state.code = None
        state.message = message
        state.action = None
        state.checked_at = now
        state.last_healthy_at = now
        self._clear_pending_failure(state)
        self._persist_notification_transition(state)

    def unhealthy(
        self,
        name: str,
        code: str,
        message: str,
        *,
        action: Optional[str] = None,
    ) -> None:
        state = self.sources[name]
        now = _utcnow()
        state.checked_at = now

        if state.status != "unhealthy" and code not in IMMEDIATE_UNHEALTHY_CODES:
            state.consecutive_failures += 1
            state.pending_code = code
            state.pending_message = message
            state.pending_action = action
            state.pending_failure_at = now
            if state.consecutive_failures < TRANSIENT_FAILURE_THRESHOLD:
                return

        state.status = "unhealthy"
        state.code = code
        state.message = message
        state.action = action
        self._clear_pending_failure(state)
        self._persist_notification_transition(state)

    @staticmethod
    def _clear_pending_failure(state: SourceHealthState) -> None:
        state.consecutive_failures = 0
        state.pending_code = None
        state.pending_message = None
        state.pending_action = None
        state.pending_failure_at = None

    def _persist_notification_transition(self, state: SourceHealthState) -> None:
        desired = state.status
        if desired not in {"healthy", "unhealthy"}:
            return
        if state.last_notified_status == desired:
            return

        should_emit = desired == "unhealthy" or state.last_notified_status == "unhealthy"
        event_count = 0
        try:
            with self.services.db_session_maker() as session:
                self.services.kv.set_in_session(
                    session,
                    state.source_id,
                    HEALTH_NOTIFICATION_KEY,
                    desired,
                )
                if should_emit and self.internal_source_id is not None:
                    event_type = (
                        "inboxclaw.source.unhealthy"
                        if desired == "unhealthy"
                        else "inboxclaw.source.recovered"
                    )
                    event_count = self.services.writer.write_events_in_session(
                        session,
                        self.internal_source_id,
                        [
                            NewEvent(
                                event_id=f"health:{state.source_id}:{desired}:{uuid.uuid4()}",
                                event_type=event_type,
                                entity_id=state.name,
                                occurred_at=state.checked_at,
                                data={
                                    "source_name": state.name,
                                    "source_type": state.source_type,
                                    "status": desired,
                                    "code": state.code,
                                    "message": state.message,
                                    "action": state.action,
                                    "checked_at": state.checked_at.isoformat() if state.checked_at else None,
                                },
                            )
                        ],
                    )
                session.commit()
            state.last_notified_status = desired
            if event_count:
                self.services.notifier.notify()
        except Exception:
            logger.exception("Failed to persist health notification state for source '%s'", state.name)

    async def watchdog(self) -> None:
        """Detect source tasks that crash or stop reporting; never call upstream APIs."""
        while True:
            await asyncio.sleep(5)
            if self._stopping:
                return
            now = _utcnow()
            for state in list(self.sources.values()):
                if state.task and state.task.done() and not state.task.cancelled():
                    continue  # The task callback reports the failure.
                if state.expected_interval is None:
                    continue

                grace = max(60.0, state.expected_interval * 2.0 + 30.0)
                reference = max(
                    value
                    for value in (state.checked_at, state.last_started_at, state.registered_at)
                    if value is not None
                )
                if (now - reference).total_seconds() <= grace:
                    continue
                if state.status == "unhealthy":
                    continue
                code = "not_reporting" if state.checked_at is None else "stale"
                self.unhealthy(
                    state.name,
                    code,
                    "The source has not reported a completed operation within its expected interval.",
                )

    def snapshot(self) -> dict[str, Any]:
        source_rows = [state.to_dict() for state in sorted(self.sources.values(), key=lambda s: s.name)]
        if any(row["status"] == "unhealthy" for row in source_rows):
            status = "unhealthy"
        elif any(row["status"] == "starting" for row in source_rows):
            status = "starting"
        else:
            status = "healthy"
        return {
            "status": status,
            "checked_at": _utcnow().isoformat(),
            "sources": source_rows,
            "failures": [row for row in source_rows if row["status"] == "unhealthy"],
        }

    def stop(self) -> None:
        self._stopping = True
