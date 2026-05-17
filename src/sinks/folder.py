import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Union

from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.orm import joinedload

from src.config import FolderSinkConfig
from src.database import Event
from src.pipeline.matcher import EventMatcher
from src.schemas import EventWithMeta
from src.services import AppServices

logger = logging.getLogger(__name__)


class FolderSink:
    def __init__(
        self,
        name: str,
        config: Union[FolderSinkConfig, Dict[str, Any]],
        services: AppServices,
    ):
        if isinstance(config, dict):
            try:
                config = FolderSinkConfig(**config)
            except ValidationError as e:
                for error in e.errors():
                    if error["type"] == "missing":
                        raise KeyError(f"'{error['loc'][0]}'")
                raise e

        self.name = name
        self.services = services
        self.config = config
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

        os.makedirs(self.config.path, exist_ok=True)
        self._task = self.services.add_task(self._run_loop())
        logger.info("Folder sink '%s' started (path=%s)", self.name, self.config.path)

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
            logger.info("Folder sink '%s' stopped", self.name)

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
            logger.exception("Error in folder sink '%s' loop", self.name)
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

        created_at = event.created_at
        if created_at is None:
            created_at = datetime.now(timezone.utc)

        filename = created_at.strftime("%Y-%m-%d") + ".jsonl"
        filepath = os.path.join(self.config.path, filename)

        try:
            os.makedirs(self.config.path, exist_ok=True)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.exception(
                "Folder sink '%s' failed to write event %s to %s",
                self.name,
                event.event_id,
                filepath,
            )

    def _get_last_event_id(self) -> int:
        with self.services.db_session_maker() as session:
            try:
                stmt = select(func.max(Event.id))
                return session.scalar(stmt) or 0
            except Exception:
                return 0
