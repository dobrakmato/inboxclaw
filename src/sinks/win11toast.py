import asyncio
import json
import logging
from typing import Any, Dict, Union

from pydantic import ValidationError
from sqlalchemy import select

from src.config import Win11ToastSinkConfig
from src.database import Event
from src.pipeline.matcher import EventMatcher
from src.services import AppServices

_WIN11TOAST_AVAILABLE = True
_WIN11TOAST_IMPORT_ERROR: ImportError | None = None

try:
    from win11toast import toast as win11_toast
except ImportError as import_error:  # pragma: no cover - environment dependent
    _WIN11TOAST_AVAILABLE = False
    _WIN11TOAST_IMPORT_ERROR = import_error

    def win11_toast(_title: str, _body: str) -> None:
        raise RuntimeError("win11toast is unavailable")

logger = logging.getLogger(__name__)


class Win11ToastSink:
    def __init__(
        self,
        name: str,
        config: Union[Win11ToastSinkConfig, Dict[str, Any]],
        services: AppServices,
    ):
        if isinstance(config, dict):
            try:
                config = Win11ToastSinkConfig(**config)
            except ValidationError as e:
                for error in e.errors():
                    if error["type"] == "missing":
                        raise KeyError(f"'{error['loc'][0]}'")
                raise e

        self.name = name
        self.services = services
        self.config = config
        self.matcher = EventMatcher(config.match)
        self.max_body_length = config.max_body_length
        self._toast_available = _WIN11TOAST_AVAILABLE
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

        if not self._toast_available:
            logger.warning(
                "Win11 toast sink '%s' is disabled: win11toast import failed (%s). "
                "Install dependencies (`uv sync`) on Windows 11 to enable notifications.",
                self.name,
                _WIN11TOAST_IMPORT_ERROR,
            )
            return

        self._task = self.services.add_task(self._run_loop())
        logger.info("Win11 toast sink '%s' started", self.name)

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
            logger.info("Win11 toast sink '%s' stopped", self.name)

    async def _run_loop(self) -> None:
        notification_event = None
        try:
            notification_event = self.services.notifier.subscribe()
            while True:
                await notification_event.wait()
                notification_event.clear()
                self._last_event_id = self.process_new_events(self._last_event_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in Win11 toast sink '%s' loop", self.name)
        finally:
            if notification_event is not None:
                self.services.notifier.unsubscribe(notification_event)

    def process_new_events(self, last_id: int) -> int:
        events = self._load_new_events(last_id)
        if not events:
            return last_id

        for event in events:
            if self.matcher.matches(event.event_type):
                self._show_toast(event)

        return events[-1].id

    def _load_new_events(self, last_id: int) -> list[Event]:
        with self.services.db_session_maker() as session:
            stmt = (
                select(Event)
                .where(Event.id > last_id)
                .order_by(Event.id.asc())
            )
            return list(session.scalars(stmt).all())

    def _show_toast(self, event: Event) -> None:
        if not self._toast_available:
            return

        title = event.event_type
        body = self._summarize_event(event)
        try:
            win11_toast(title, body)
        except RuntimeError as e:
            logger.warning(
                "Win11 toast sink '%s' runtime error while showing notification: %s",
                self.name,
                e,
            )
        except Exception:
            logger.exception(
                "Win11 toast sink '%s' failed to show notification for event %s",
                self.name,
                event.event_id,
            )

    def _summarize_event(self, event: Event) -> str:
        payload = event.data
        prefix = f"entity={event.entity_id}" if event.entity_id else None

        if isinstance(payload, dict):
            summary = self._summarize_dict(payload)
        elif isinstance(payload, list):
            summary = self._summarize_list(payload)
        elif payload is None:
            summary = "No event payload"
        else:
            summary = str(payload)

        if prefix:
            summary = f"{prefix} | {summary}"

        return self._truncate(summary)

    def _summarize_dict(self, data: Dict[str, Any]) -> str:
        preferred_keys = [
            "summary",
            "title",
            "subject",
            "name",
            "filename",
            "file_name",
            "message",
            "description",
            "snippet",
            "status",
            "action",
        ]

        pieces: list[str] = []
        for key in preferred_keys:
            if key in data and self._is_brief_scalar(data[key]):
                pieces.append(f"{key}={data[key]}")
            if len(pieces) >= 3:
                break

        if not pieces:
            for key, value in self._extract_scalar_pairs(data):
                pieces.append(f"{key}={value}")
                if len(pieces) >= 3:
                    break

        if pieces:
            return "; ".join(pieces)

        return self._json_snippet(data)

    def _summarize_list(self, data: list[Any]) -> str:
        if not data:
            return "Empty list payload"

        first = data[0]
        if self._is_brief_scalar(first):
            return f"items={len(data)} first={first}"

        if isinstance(first, dict):
            first_summary = self._summarize_dict(first)
            return f"items={len(data)} first: {first_summary}"

        return self._json_snippet(data)

    def _extract_scalar_pairs(self, data: Dict[str, Any], parent: str = "") -> list[tuple[str, Any]]:
        pairs: list[tuple[str, Any]] = []
        for key, value in data.items():
            full_key = f"{parent}.{key}" if parent else key
            if self._is_brief_scalar(value):
                pairs.append((full_key, value))
                continue
            if isinstance(value, dict):
                pairs.extend(self._extract_scalar_pairs(value, parent=full_key))
        return pairs

    @staticmethod
    def _is_brief_scalar(value: Any) -> bool:
        return isinstance(value, (str, int, float, bool))

    def _json_snippet(self, value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, default=str)
        return f"data={self._truncate(raw)}"

    def _truncate(self, value: str) -> str:
        if len(value) <= self.max_body_length:
            return value
        return value[: self.max_body_length - 1].rstrip() + "…"

    def _get_last_event_id(self) -> int:
        with self.services.db_session_maker() as session:
            from sqlalchemy import func

            try:
                stmt = select(func.max(Event.id))
                return session.scalar(stmt) or 0
            except Exception:
                return 0