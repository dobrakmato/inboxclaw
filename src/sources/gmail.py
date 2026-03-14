import asyncio
import logging
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import GmailSourceConfig
from src.pipeline.writer import NewEvent
from src.services import AppServices
from src.utils.google_auth import get_google_credentials

logger = logging.getLogger(__name__)

class GmailSource:
    def __init__(self, name: str, config: GmailSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.token_file = config.token_file
        self.poll_interval = config.poll_interval

    async def fetch_and_publish(self):
        try:
            creds = get_google_credentials(self.token_file, self.name)
            # build() can be slow/blocking, but we are in an async task, so it's okay for now
            # In a highly scalable system, we might want to run it in a thread pool
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            
            # Fetch recent messages
            results = service.users().messages().list(userId='me', maxResults=self.config.max_results).execute()
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
                
                label_ids = msg.get('labelIds', [])
                
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
                        "internalDate": internal_date,
                        "labelIds": label_ids
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
            await asyncio.sleep(self.poll_interval)
