import asyncio
import logging
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import GmailSourceConfig
from src.pipeline.cursor import SourceCursor
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
        self.cursor = SourceCursor(services)

    def _get_service(self):
        creds = get_google_credentials(self.token_file, self.name)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _initialize_history_id(self, service) -> bool:
        """First run: get the latest historyId using messages().list() with 1 message."""
        logger.info(f"No cursor for {self.name}, initializing historyId")
        results = service.users().messages().list(userId='me', maxResults=1).execute()
        messages = results.get('messages', [])
        if messages:
            msg_id = messages[0]['id']
            msg = service.users().messages().get(userId='me', id=msg_id, format='minimal').execute()
            initial_history_id = msg.get('historyId')
            if initial_history_id:
                self.cursor.set_cursor(self.source_id, str(initial_history_id))
                logger.info(f"Initialized historyId for {self.name} to {initial_history_id}")
                return True
        return False

    def _get_message_metadata(self, service, msg_id: str) -> dict:
        """Fetch full message metadata."""
        return service.users().messages().get(
            userId='me',
            id=msg_id,
            format='metadata',
            metadataHeaders=['From', 'To', 'Subject', 'Date']
        ).execute()

    def _create_event(self, msg_id: str, msg: dict) -> NewEvent:
        """Construct a NewEvent from Gmail message data."""
        label_ids = msg.get('labelIds', [])
        payload = msg.get('payload', {})
        headers_list = payload.get('headers', [])
        headers = {h['name']: h['value'] for h in headers_list}

        internal_date = msg.get("internalDate")
        occurred_at = None
        if internal_date:
            occurred_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)

        return NewEvent(
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
        )

    async def fetch_and_publish(self):
        try:
            service = self._get_service()
            current_history_id = self.cursor.get_last_cursor(self.source_id)

            if not current_history_id:
                if self._initialize_history_id(service):
                    return
                # If no messages found, we can't initialize historyId yet
                return

            # Fetch history since the last historyId
            events = []
            page_token = None
            new_history_id = current_history_id

            while True:
                try:
                    history_results = service.users().history().list(
                        userId='me',
                        startHistoryId=current_history_id,
                        pageToken=page_token,
                        historyTypes=['messageAdded']
                    ).execute()
                except HttpError as e:
                    if e.resp.status == 404:
                        # historyId is too old, need to re-initialize
                        logger.warning(f"historyId {current_history_id} is too old for {self.name}, re-initializing")
                        self.cursor.set_cursor(self.source_id, "")
                        return
                    raise

                history_records = history_results.get('history', [])
                new_history_id = history_results.get('historyId', new_history_id)

                for record in history_records:
                    messages_added = record.get('messagesAdded', [])
                    for msg_added in messages_added:
                        msg_ref = msg_added.get('message')
                        if not msg_ref:
                            continue

                        msg_id = msg_ref['id']
                        msg = self._get_message_metadata(service, msg_id)
                        events.append(self._create_event(msg_id, msg))

                page_token = history_results.get('nextPageToken')
                if not page_token:
                    break

            if events:
                self.services.writer.write_events(self.source_id, events)

            if str(new_history_id) != str(current_history_id):
                self.cursor.set_cursor(self.source_id, str(new_history_id))

        except HttpError as error:
            logger.error(f"An error occurred in Gmail source {self.name}: {error}")
        except Exception as e:
            logger.error(f"Unexpected error in Gmail source {self.name}: {e}", exc_info=True)

    async def run(self):
        logger.info(f"Starting Gmail source: {self.name} polling every {self.poll_interval}")
        while True:
            await self.fetch_and_publish()
            await asyncio.sleep(self.poll_interval)
