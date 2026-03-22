import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Union

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import joinedload

from pydantic import ValidationError
from src.database import Event, HttpWebhookDelivery
from src.schemas import EventWithMeta
from src.services import AppServices
from src.pipeline.matcher import EventMatcher
from src.config import WebhookSinkConfig

logger = logging.getLogger(__name__)


class WebhookSink:
    def __init__(self, name: str, config: Union[WebhookSinkConfig, Dict[str, Any]], services: AppServices, sink_id: int):
        if isinstance(config, dict):
            try:
                config = WebhookSinkConfig(**config)
            except ValidationError as e:
                # Re-raise as KeyError for compatibility with tests
                for error in e.errors():
                    if error["type"] == "missing":
                        if error["loc"][0] == "url":
                            raise ValueError("Webhook sink requires a 'url' configuration")
                        raise KeyError(f"'{error['loc'][0]}'")
                raise e
        self.name = name
        self.services = services
        self.config = config
        self.sink_id = sink_id

        self.url = config.url
        self.headers = config.headers
        self.matcher = EventMatcher(config.match)
        self.max_retries = config.max_retries
        self.retry_interval = config.retry_interval
        self._task: asyncio.Task | None = None

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

        self._task = self.services.add_task(self._run_loop())
        logger.info("Webhook sink '%s' started, targeting %s", self.name, self.url)

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
            logger.info("Webhook sink '%s' stopped", self.name)

    async def _run_loop(self):
        notification_event = None
        try:
            notification_event = self.services.notifier.subscribe()
            while True:
                await self.process_pending_events()
                try:
                    await asyncio.wait_for(notification_event.wait(), timeout=self.retry_interval)
                    notification_event.clear()
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in Webhook sink '%s' loop", self.name)
        finally:
            if notification_event is not None:
                self.services.notifier.unsubscribe(notification_event)

    async def process_pending_events(self) -> None:
        events = self._load_pending_events()
        if not events:
            return

        logger.info(
            "Webhook sink '%s' found %d events to deliver",
            self.name,
            len(events),
        )

        async with httpx.AsyncClient() as client:
            for event in events:
                await self._deliver_one_event(client, event)

    def _load_pending_events(self) -> list[Event]:
        with self.services.db_session_maker() as session:
            stmt = (
                select(Event)
                .options(joinedload(Event.source))
                .outerjoin(
                    HttpWebhookDelivery,
                    (HttpWebhookDelivery.event_id == Event.id) & (HttpWebhookDelivery.sink_id == self.sink_id)
                )
                .where(self._not_delivered_clause())
                .where(self._retryable_clause())
                .where(self.matcher.build_sqlalchemy_clause())
                .where(EventMatcher.build_ttl_clause(
                    self.config.ttl_enabled,
                    self.config.default_ttl,
                    self.config.event_ttl
                ))
                .order_by(Event.created_at.asc())
            )
            return list(session.scalars(stmt).all())

    def _not_delivered_clause(self):
        return or_(
            HttpWebhookDelivery.id.is_(None),
            HttpWebhookDelivery.delivered.is_(False),
        )

    def _retryable_clause(self):
        retry_cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.retry_interval)
        return or_(
            HttpWebhookDelivery.id.is_(None),
            and_(
                HttpWebhookDelivery.tries < self.max_retries,
                or_(
                    HttpWebhookDelivery.last_try.is_(None),
                    HttpWebhookDelivery.last_try <= retry_cutoff,
                ),
            ),
        )


    async def _deliver_one_event(self, client: httpx.AsyncClient, event: Event) -> None:
        payload = self._build_payload(event)
        delivered = await self._post_payload(client, event, payload)
        self._record_delivery_attempt(event.id, delivered)

    async def _post_payload(
        self,
        client: httpx.AsyncClient,
        event: Event,
        payload: dict[str, Any],
    ) -> bool:
        try:
            logger.debug(
                "Webhook sink '%s' delivering event %s to %s",
                self.name,
                event.event_id,
                self.url,
            )
            response = await client.post(
                self.url,
                json=payload,
                headers=self.headers,
                timeout=10.0
            )
        except Exception:
            logger.exception(
                "Webhook sink '%s' error delivering event %s",
                self.name,
                event.event_id,
            )
            return False

        if 200 <= response.status_code < 300:
            logger.info(
                "Webhook sink '%s' delivered event %s successfully",
                self.name,
                event.event_id,
            )
            return True

        logger.warning(
            "Webhook sink '%s' failed to deliver event %s. Status=%s Response=%s Payload=%s",
            self.name,
            event.event_id,
            response.status_code,
            response.text,
            payload,
        )
        return False

    def _record_delivery_attempt(self, event_id: int, delivered: bool) -> None:
        with self.services.db_session_maker() as session:
            delivery = session.scalar(
                select(HttpWebhookDelivery).where(
                    (HttpWebhookDelivery.event_id == event_id) & (HttpWebhookDelivery.sink_id == self.sink_id)
                )
            )

            if delivery is None:
                delivery = HttpWebhookDelivery(
                    event_id=event_id,
                    sink_id=self.sink_id,
                    tries=0,
                )
                session.add(delivery)

            delivery.tries += 1
            delivery.last_try = datetime.now(timezone.utc)
            delivery.delivered = delivered

            session.commit()

    def _build_payload(self, event: Event) -> dict[str, Any]:
        default_payload = EventWithMeta.from_event(event).to_dict()
        if not self.config.payload:
            return default_payload

        return self._transform_payload(self.config.payload, {"root": default_payload})

    def _transform_payload(self, template: Any, context: dict) -> Any:
        if isinstance(template, dict):
            return {k: self._transform_payload(v, context) for k, v in template.items()}
        elif isinstance(template, list):
            return [self._transform_payload(i, context) for i in template]
        elif isinstance(template, str):
            # Special case: if the whole string is a path (old behavior, supports non-string returns)
            if template.startswith("#") and " " not in template:
                return self._resolve_path(template[1:], context)
            if template.startswith("$") and " " not in template:
                import json
                val = self._resolve_path(template[1:], context)
                return json.dumps(val)

            # String interpolation
            import re
            import json

            def replace_match(match):
                prefix = match.group(1)
                path = match.group(2)
                val = self._resolve_path(path, context)
                if prefix == "#":
                    return str(val) if val is not None else ""
                else:  # prefix == "$"
                    return json.dumps(val) if val is not None else "null"

            # Regex to find #path.to.field or $path.to.field
            # We assume paths are alphanumeric with dots, starting with root
            # This matches #root.something or $root.something
            pattern = r"([#\$])(root(?:\.[a-zA-Z0-9_]+)*)"
            return re.sub(pattern, replace_match, template)

        return template

    def _resolve_path(self, path: str, context: dict) -> Any:
        parts = path.split(".")
        current = context
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current