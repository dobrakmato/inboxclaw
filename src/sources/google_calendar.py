import asyncio
import logging
from datetime import datetime, timezone

import pytimeparse
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import update

from src.config import GoogleCalendarSourceConfig
from src.database import Source
from src.pipeline.writer import NewEvent
from src.services import AppServices

logger = logging.getLogger(__name__)

class GoogleCalendarSource:
    def __init__(self, name: str, config: GoogleCalendarSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.token_file = config.token_file
        self.poll_interval = config.poll_interval
        self.poll_interval_seconds = pytimeparse.parse(self.poll_interval) or 600
        self.calendar_id = config.calendar_id

    def get_credentials(self) -> Credentials:
        if not self.token_file:
            raise ValueError(f"No token_file configured for Google Calendar source {self.name}")
        
        creds = Credentials.from_authorized_user_file(self.token_file)
        
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(self.token_file, "w") as f:
                f.write(creds.to_json())
        
        return creds

    async def fetch_and_publish(self):
        try:
            creds = self.get_credentials()
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            
            with self.services.db_session_maker() as session:
                source = session.get(Source, self.source_id)
                sync_token = source.cursor

            page_token = None
            
            while True:
                kwargs = {
                    "calendarId": self.calendar_id,
                    "pageToken": page_token,
                }
                
                if sync_token:
                    kwargs["syncToken"] = sync_token
                else:
                    # If no sync token, fetch events from now onwards
                    now = datetime.now(timezone.utc).isoformat()
                    kwargs["timeMin"] = now

                try:
                    events_result = service.events().list(**kwargs).execute()
                except HttpError as e:
                    if e.resp.status == 410:
                        # Sync token expired, clear it and start over
                        logger.warning(f"Sync token expired for {self.name}, clearing and restarting sync")
                        with self.services.db_session_maker() as session:
                            session.execute(
                                update(Source).where(Source.id == self.source_id).values(cursor=None)
                            )
                            session.commit()
                        sync_token = None
                        page_token = None
                        continue
                    else:
                        raise

                events = events_result.get('items', [])
                
                events_to_write = []
                for event_item in events:
                    event_id = event_item.get('id')
                    # For calendar, event_id + updated time for unique events
                    updated = event_item.get('updated')
                    internal_event_id = f"cal-{event_id}-{updated or 'N/A'}"
                    
                    occurred_at = None
                    if updated:
                        occurred_at = datetime.fromisoformat(updated.replace('Z', '+00:00'))

                    events_to_write.append(NewEvent(
                        event_id=internal_event_id,
                        event_type="calendar.event_change",
                        entity_id=event_id,
                        data=event_item,
                        occurred_at=occurred_at
                    ))
                
                self.services.writer.write_events(self.source_id, events_to_write)

                page_token = events_result.get('nextPageToken')
                if not page_token:
                    new_sync_token = events_result.get('nextSyncToken')
                    if new_sync_token:
                        with self.services.db_session_maker() as session:
                            session.execute(
                                update(Source).where(Source.id == self.source_id).values(cursor=new_sync_token)
                            )
                            session.commit()
                    break

        except HttpError as error:
            logger.error(f"An error occurred in Google Calendar source {self.name}: {error}")
        except Exception as e:
            logger.error(f"Unexpected error in Google Calendar source {self.name}: {e}", exc_info=True)

    async def run(self):
        logger.info(f"Starting Google Calendar source: {self.name} polling every {self.poll_interval}")
        while True:
            await self.fetch_and_publish()
            await asyncio.sleep(self.poll_interval_seconds)
