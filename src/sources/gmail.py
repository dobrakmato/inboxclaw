import logging
from typing import Any, Dict
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker
from src.services import AppServices
from src.database import Event
import uuid

logger = logging.getLogger(__name__)

class GmailSource:
    def __init__(self, name: str, config: Dict[str, Any], services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.setup_endpoints()

    def setup_endpoints(self):
        # Register any required endpoints for push notifications (e.g., Google Pub/Sub)
        @self.services.app.post(f"/source/{self.name}/webhook")
        async def webhook(data: Dict[str, Any]):
            logger.info(f"Received webhook for {self.name}")
            
            # Record the event in the database
            with self.services.db_session_maker() as session:
                event = Event(
                    event_id=str(uuid.uuid4()), # In reality, Gmail message ID or similar
                    source_id=self.source_id,
                    event_type="gmail.notification",
                    entity_id=data.get("email_id", "unknown"),
                    data=data
                )
                session.add(event)
                session.commit()
                logger.info(f"Stored event {event.event_id} from source {self.name}")

            # Notify sinks
            self.services.notifier.notify()
            return {"status": "accepted"}

    def run(self):
        # Implementation for polling or other background tasks
        logger.info(f"Starting Gmail source: {self.name}")
        pass
