import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Union, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import joinedload
from pydantic import ValidationError

from src.database import Event, CommandSinkDelivery
from src.schemas import EventWithMeta
from src.services import AppServices
from src.pipeline.matcher import EventMatcher
from src.config import CommandSinkConfig
from src.utils.template import transform_template

logger = logging.getLogger(__name__)

class CommandSink:
    def __init__(self, name: str, config: Union[CommandSinkConfig, Dict[str, Any]], services: AppServices, sink_id: int):
        if isinstance(config, dict):
            try:
                config = CommandSinkConfig(**config)
            except ValidationError as e:
                raise KeyError(str(e))
        
        self.name = name
        self.config = config
        self.services = services
        self.sink_id = sink_id
        self.matcher = EventMatcher(config.match)
        
        self.queue = asyncio.Queue()
        self._consecutive_failures = 0
        self._breaker_until: Optional[datetime] = None
        self._processing_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def match(self, event: Event) -> bool:
        return self.matcher.matches(event.event_type)

    def start(self):
        # Start background processor
        self._processing_task = self.services.add_task(self._processor())
        # Start the notifier listener
        self.services.add_task(self._listen_notifier())
        # Also check for missed/retry events on start
        self.services.add_task(self._initial_load())
        # Start a periodic retry loop
        self.services.add_task(self._retry_loop())

    async def _listen_notifier(self):
        notifier_event = self.services.notifier.subscribe()
        try:
            while not self._stop_event.is_set():
                await notifier_event.wait()
                notifier_event.clear()
                await self._queue_pending_events()
        finally:
            self.services.notifier.unsubscribe(notifier_event)

    async def _initial_load(self):
        """Load pending events from DB on startup."""
        await self._queue_pending_events()

    async def _retry_loop(self):
        """Periodically check for events that need retry."""
        while not self._stop_event.is_set():
            await asyncio.sleep(60) # Check every minute
            await self._queue_pending_events()

    async def _queue_pending_events(self):
        with self.services.db_session_maker() as session:
            # Events not yet delivered or failed and needing retry
            retry_cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.config.retry_interval)
            
            # Match internal patterns + TTL logic
            match_clause = self.matcher.build_sqlalchemy_clause()
            ttl_clause = EventMatcher.build_ttl_clause(
                self.config.ttl_enabled,
                self.config.default_ttl,
                self.config.event_ttl
            )

            stmt = select(Event.id).outerjoin(
                CommandSinkDelivery, 
                and_(
                    CommandSinkDelivery.event_id == Event.id,
                    CommandSinkDelivery.sink_id == self.sink_id
                )
            ).where(
                and_(
                    match_clause,
                    ttl_clause,
                    or_(
                        CommandSinkDelivery.id.is_(None),
                        and_(
                            CommandSinkDelivery.processed == False,
                            CommandSinkDelivery.tries < self.config.max_retries,
                            or_(
                                CommandSinkDelivery.last_try.is_(None),
                                CommandSinkDelivery.last_try <= retry_cutoff
                            )
                        )
                    )
                )
            ).order_by(Event.created_at.asc())

            event_ids = session.scalars(stmt).all()
            
            for eid in event_ids:
                # Basic avoidance of flooding the queue if it's already full of the same IDs
                # But since we're using event_ids from DB, and they'll be processed soon, it's okay.
                await self.queue.put(eid)

    async def _processor(self):
        logger.info("Command sink '%s' processor started", self.name)
        while not self._stop_event.is_set():
            # Wait for events
            try:
                event_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # Check circuit breaker before processing
            if self._breaker_until and datetime.now(timezone.utc) < self._breaker_until:
                # Re-queue the event we just took out and wait
                await self.queue.put(event_id)
                self.queue.task_done()
                wait_time = (self._breaker_until - datetime.now(timezone.utc)).total_seconds()
                logger.warning("Command sink '%s' circuit breaker active. Waiting %ds", self.name, wait_time)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=min(wait_time, 1.0))
                except asyncio.TimeoutError:
                    pass
                continue
            
            # Drain queue if we have many events to trigger batching
            batch = [event_id]
            while self.queue.qsize() > 0 and len(batch) < 100: # Cap batch size
                # Peek at the circuit breaker state again
                if self._breaker_until and datetime.now(timezone.utc) < self._breaker_until:
                    break
                batch.append(self.queue.get_nowait())
            
            # Final check before processing any command
            if self._breaker_until and datetime.now(timezone.utc) < self._breaker_until:
                for eid in batch:
                    await self.queue.put(eid)
                    self.queue.task_done()
                continue
            
            try:
                if len(batch) >= self.config.batch_threshold:
                    await self._process_batch_ids(batch)
                else:
                    for eid in batch:
                        if self._breaker_until and datetime.now(timezone.utc) < self._breaker_until:
                            await self.queue.put(eid)
                            continue
                        await self._process_one_id(eid)
            except Exception:
                logger.exception("Error in command sink '%s' processor", self.name)
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def _process_one_id(self, event_id: int):
        event = self._load_event(event_id)
        if not event:
            return

        if not self.match(event):
            # Record that we matched it but decided not to process? 
            # Actually if it doesn't match the sink, it shouldn't even be here.
            # But matcher can change, or we might have over-queued.
            # Let's record it as processed so we don't pick it up again.
            self._record_result(event.id, processed=True, return_code=0)
            return

        context = {"root": EventWithMeta.from_event(event).to_dict()}
        cmd_str = transform_template(self.config.command, context, shell_quote=True)
        
        logger.debug("Command sink '%s' executing: %s", self.name, cmd_str)
        
        res = await self._run_command(cmd_str)
        success = res["return_code"] == 0
        
        self._record_result(event.id, processed=success, return_code=res["return_code"])
        self._update_breaker(success)

    async def _process_batch_ids(self, event_ids: List[int]):
        events = []
        for eid in event_ids:
            e = self._load_event(eid)
            if e:
                if not self.match(e):
                    self._record_result(e.id, processed=True, return_code=0)
                    continue
                events.append(e)
        
        if not events:
            return

        template = self.config.batch_command or self.config.command
        event_dicts = [EventWithMeta.from_event(e).to_dict() for e in events]
        context = {"root": event_dicts}
        
        cmd_str = transform_template(template, context, shell_quote=True)
        logger.info("Command sink '%s' executing batch (%d events): %s", self.name, len(events), cmd_str)
        
        res = await self._run_command(cmd_str)
        success = res["return_code"] == 0
        
        for e in events:
            self._record_result(e.id, processed=success, return_code=res["return_code"])
        
        self._update_breaker(success)

    def _load_event(self, event_id: int) -> Optional[Event]:
        with self.services.db_session_maker() as session:
            return session.get(Event, event_id, options=[joinedload(Event.source)])

    async def _run_command(self, cmd: str) -> Dict[str, Any]:
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error("Command failed with exit code %d: %s", process.returncode, cmd)
                if stderr:
                    logger.error("Stderr: %s", stderr.decode().strip())

            return {
                "return_code": process.returncode,
            }
        except Exception as e:
            logger.exception("Failed to run command: %s", cmd)
            return {
                "return_code": -1,
            }

    def _record_result(self, event_id: int, processed: bool, return_code: int):
        with self.services.db_session_maker() as session:
            delivery = session.scalar(
                select(CommandSinkDelivery).where(
                    and_(
                        CommandSinkDelivery.event_id == event_id,
                        CommandSinkDelivery.sink_id == self.sink_id
                    )
                )
            )
            
            if delivery is None:
                delivery = CommandSinkDelivery(
                    event_id=event_id,
                    sink_id=self.sink_id,
                    tries=0
                )
                session.add(delivery)
            
            delivery.tries += 1
            delivery.last_try = datetime.now(timezone.utc)
            delivery.processed = processed
            delivery.processed_at = datetime.now(timezone.utc)
            delivery.return_code = return_code
            
            session.commit()

    def _update_breaker(self, success: bool):
        if success:
            self._consecutive_failures = 0
            self._breaker_until = None
        else:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 5:
                self._breaker_until = datetime.now(timezone.utc) + timedelta(minutes=10)
                logger.error("Command sink '%s' circuit breaker triggered! No commands for 10 minutes.", self.name)
