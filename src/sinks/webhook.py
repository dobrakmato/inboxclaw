import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session

from src.database import Event, HttpWebhookDelivery
from src.services import AppServices
from src.pipeline.matcher import EventMatcher
from src.config import WebhookSinkConfig

logger = logging.getLogger(__name__)


class WebhookSink:
    def __init__(self, name: str, config: WebhookSinkConfig, services: AppServices):
        self.name = name
        self.services = services
        self.config = config

        self.url = config.url
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
                .outerjoin(HttpWebhookDelivery, HttpWebhookDelivery.event_id == Event.id)
                .where(self._not_delivered_clause())
                .where(self._retryable_clause())
                .where(self.matcher.build_sqlalchemy_clause())
                .order_by(Event.created_at.asc())
            )
            return list(session.scalars(stmt).all())

    def _not_delivered_clause(self):
        return or_(
            HttpWebhookDelivery.id.is_(None),
            HttpWebhookDelivery.delivered.is_(False),
        )

    def _retryable_clause(self):
        return or_(
            HttpWebhookDelivery.id.is_(None),
            HttpWebhookDelivery.tries < self.max_retries,
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
            response = await client.post(self.url, json=payload, timeout=10.0)
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
            "Webhook sink '%s' failed to deliver event %s. Status=%s Response=%s",
            self.name,
            event.event_id,
            response.status_code,
            response.text,
        )
        return False

    def _record_delivery_attempt(self, event_id: int, delivered: bool) -> None:
        with self.services.db_session_maker() as session:
            delivery = session.scalar(
                select(HttpWebhookDelivery).where(HttpWebhookDelivery.event_id == event_id)
            )

            if delivery is None:
                delivery = HttpWebhookDelivery(
                    event_id=event_id,
                    tries=0,
                )
                session.add(delivery)

            delivery.tries += 1
            delivery.last_try = datetime.now(timezone.utc)
            delivery.delivered = delivered

            session.commit()

    def _build_payload(self, event: Event) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "entity_id": event.entity_id,
            "created_at": event.created_at.isoformat() if event.created_at else None,
            "data": event.data,
            "meta": event.meta,
        }