import asyncio
import logging
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import update

from src.config import GoogleDriveSourceConfig
from src.database import Source
from src.pipeline.writer import NewEvent
from src.services import AppServices
from src.utils.google_auth import get_google_credentials

logger = logging.getLogger(__name__)

class GoogleDriveSource:
    def __init__(self, name: str, config: GoogleDriveSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.token_file = config.token_file
        self.poll_interval = config.poll_interval

    async def fetch_and_publish(self):
        try:
            creds = get_google_credentials(self.token_file, self.name)
            service = build("drive", "v3", credentials=creds, cache_discovery=False)
            
            # Get current cursor (startPageToken) if not exists
            with self.services.db_session_maker() as session:
                source = session.get(Source, self.source_id)
                page_token = source.cursor

            if not page_token:
                response = service.changes().getStartPageToken().execute()
                page_token = response.get('startPageToken')
                logger.info(f"Initialized Google Drive startPageToken: {page_token} for {self.name}")
                with self.services.db_session_maker() as session:
                    session.execute(
                        update(Source).where(Source.id == self.source_id).values(cursor=page_token)
                    )
                    session.commit()

            while page_token:
                response = service.changes().list(pageToken=page_token, spaces='drive').execute()
                
                changes = response.get('changes', [])
                events = []
                for change in changes:
                    file_id = change.get('fileId')
                    change_time = change.get('time')
                    removed = change.get('removed', False)
                    
                    event_id = f"drive-{file_id}-{change_time or 'N/A'}"
                    
                    file_metadata = {}
                    if not removed:
                        try:
                            file_metadata = service.files().get(fileId=file_id, fields='id, name, mimeType, modifiedTime, owners').execute()
                        except HttpError as e:
                            if e.resp.status == 404:
                                logger.warning(f"File {file_id} not found (might have been deleted shortly after change)")
                            else:
                                raise

                    occurred_at = None
                    if change_time:
                        occurred_at = datetime.fromisoformat(change_time.replace('Z', '+00:00'))

                    events.append(NewEvent(
                        event_id=event_id,
                        event_type="drive.file_change",
                        entity_id=file_id,
                        data={
                            "fileId": file_id,
                            "removed": removed,
                            "time": change_time,
                            "file": file_metadata
                        },
                        occurred_at=occurred_at
                    ))

                self.services.writer.write_events(self.source_id, events)

                if 'nextPageToken' in response:
                    page_token = response.get('nextPageToken')
                else:
                    new_start_page_token = response.get('newStartPageToken')
                    if new_start_page_token:
                        with self.services.db_session_maker() as session:
                            session.execute(
                                update(Source).where(Source.id == self.source_id).values(cursor=new_start_page_token)
                            )
                            session.commit()
                        page_token = None # End of current changes
                    else:
                        break
                
        except HttpError as error:
            logger.error(f"An error occurred in Google Drive source {self.name}: {error}")
        except Exception as e:
            logger.error(f"Unexpected error in Google Drive source {self.name}: {e}", exc_info=True)

    async def run(self):
        logger.info(f"Starting Google Drive source: {self.name} polling every {self.poll_interval}")
        while True:
            await self.fetch_and_publish()
            await asyncio.sleep(self.poll_interval)
