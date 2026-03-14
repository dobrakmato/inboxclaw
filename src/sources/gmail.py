import asyncio
import logging
from datetime import datetime, timezone

import pytimeparse
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import GmailSourceConfig
from src.pipeline.writer import NewEvent
from src.services import AppServices

logger = logging.getLogger(__name__)

class GmailSource:
    def __init__(self, name: str, config: GmailSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.token_file = config.token_file
        self.poll_interval = config.poll_interval
        self.poll_interval_seconds = pytimeparse.parse(self.poll_interval) or 60

    def get_credentials(self) -> Credentials:
        if not self.token_file:
            raise ValueError(f"No token_file configured for Gmail source {self.name}")
        
        creds = Credentials.from_authorized_user_file(self.token_file)
        
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())
        
        return creds

    async def fetch_and_publish(self):
        try:
            creds = self.get_credentials()
            # build() can be slow/blocking, but we are in an async task, so it's okay for now
            # In a highly scalable system, we might want to run it in a thread pool
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            
            # Fetch recent 50 messages
            results = service.users().messages().list(userId='me', maxResults=50).execute()
            messages = results.get('messages', [])
            
            if not messages:
                logger.info(f"No messages found for {self.name}")
                return

            events = []
            for msg_ref in messages:
                msg_id = msg_ref['id']
                
                # Fetch full message metadata
                msg = service.users().messages().get(
                    userId='me', 
                    id=msg_id, 
                    format='metadata',
                    metadataHeaders=['From', 'To', 'Subject', 'Date']
                ).execute()
                
                payload = msg.get('payload', {})
                headers_list = payload.get('headers', [])
                headers = {h['name']: h['value'] for h in headers_list}
                
                internal_date = msg.get("internalDate")
                occurred_at = None
                if internal_date:
                    occurred_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)

                events.append(NewEvent(
                    event_id=msg_id,
                    event_type="gmail.email",
                    entity_id=msg_id,
                    data={
                        "threadId": msg.get("threadId"),
                        "snippet": msg.get("snippet"),
                        "from": headers.get("From"),
                        "to": headers.get("To"),
                        "subject": headers.get("Subject"),
                        "date": headers.get("Date"),
                        "internalDate": internal_date
                    },
                    occurred_at=occurred_at
                ))

            self.services.writer.write_events(self.source_id, events)
                
        except HttpError as error:
            logger.error(f"An error occurred in Gmail source {self.name}: {error}")
        except Exception as e:
            logger.error(f"Unexpected error in Gmail source {self.name}: {e}", exc_info=True)

    async def run(self):
        logger.info(f"Starting Gmail source: {self.name} polling every {self.poll_interval}")
        while True:
            await self.fetch_and_publish()
            await asyncio.sleep(self.poll_interval_seconds)
