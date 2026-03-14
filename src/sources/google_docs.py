import asyncio
import logging
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import GoogleDocsSourceConfig
from src.pipeline.writer import NewEvent
from src.services import AppServices
from src.utils.google_auth import get_google_credentials

logger = logging.getLogger(__name__)

class GoogleDocsSource:
    def __init__(self, name: str, config: GoogleDocsSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.token_file = config.token_file
        self.poll_interval = config.poll_interval

    async def fetch_and_publish(self):
        """
        Google Docs API doesn't have a 'list changes' endpoint like Drive.
        We rely on Google Drive changes with a filter for Docs, or we can poll specific docs if configured.
        For a general 'Docs' source, it's often better to use Drive API to find recently modified Docs.
        """
        try:
            creds = get_google_credentials(self.token_file, self.name)
            # We use Drive API to list recent Google Docs
            drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
            
            # Search for Google Docs modified in the last 24 hours (or since last poll)
            # mimeType = 'application/vnd.google-apps.document'
            query = "mimeType = 'application/vnd.google-apps.document' and trashed = false"
            
            # We could use the cursor to only get things modified since last time
            last_mod_time = self.services.cursor.get_last_cursor(self.source_id)
            
            if last_mod_time:
                query += f" and modifiedTime > '{last_mod_time}'"
            
            results = drive_service.files().list(
                q=query,
                fields="nextPageToken, files(id, name, modifiedTime, version)",
                orderBy="modifiedTime desc"
            ).execute()
            
            files = results.get('files', [])
            
            latest_mod = last_mod_time
            
            events = []
            for file in files:
                file_id = file.get('id')
                mod_time = file.get('modifiedTime')
                version = file.get('version', '0')
                
                event_id = f"docs-{file_id}-{mod_time or 'N/A'}-{version}"
                
                occurred_at = None
                if mod_time:
                    occurred_at = datetime.fromisoformat(mod_time.replace('Z', '+00:00'))

                events.append(NewEvent(
                    event_id=event_id,
                    event_type="docs.document_change",
                    entity_id=file_id,
                    data=file,
                    occurred_at=occurred_at
                ))
                
                if not latest_mod or (mod_time and mod_time > latest_mod):
                    latest_mod = mod_time

            new_count = self.services.writer.write_events(self.source_id, events)

            if latest_mod and latest_mod != last_mod_time:
                self.services.cursor.set_cursor(self.source_id, latest_mod)
                
        except HttpError as error:
            logger.error(f"An error occurred in Google Docs source {self.name}: {error}")
        except Exception as e:
            logger.error(f"Unexpected error in Google Docs source {self.name}: {e}", exc_info=True)

    async def run(self):
        logger.info(f"Starting Google Docs source: {self.name} polling every {self.poll_interval}")
        while True:
            await self.fetch_and_publish()
            await asyncio.sleep(self.poll_interval)
