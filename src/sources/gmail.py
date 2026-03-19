import asyncio
import logging
from datetime import datetime, timezone
from email.utils import parseaddr

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import GmailSourceConfig
from src.pipeline.cursor import SourceCursor
from src.schemas import NewEvent
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
        """First run: get the current historyId using getProfile()."""
        logger.info(f"No cursor for {self.name}, initializing historyId")
        profile = service.users().getProfile(userId='me').execute()
        initial_history_id = profile.get('historyId')
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

    def _create_message_event(self, msg_id: str, msg: dict) -> NewEvent:
        """Construct a NewEvent (message_sent or message_received) from Gmail message data."""
        label_ids = msg.get('labelIds', [])
        payload = msg.get('payload', {})
        headers_list = payload.get('headers', [])
        headers = {h['name']: h['value'] for h in headers_list}

        def parse_address(header_value: str | None) -> dict:
            if not header_value:
                return {"name": "", "email": ""}
            name, email = parseaddr(header_value)
            return {"name": name, "email": email}

        internal_date = msg.get("internalDate")
        occurred_at = datetime.now(timezone.utc)
        if internal_date:
            occurred_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)

        event_type = "gmail.message_sent" if "SENT" in label_ids else "gmail.message_received"

        return NewEvent(
            event_id=msg_id,
            event_type=event_type,
            entity_id=msg_id,
            data={
                "threadId": msg.get("threadId"),
                "messageId": msg_id,
                "snippet": msg.get("snippet"),
                "from": parse_address(headers.get("From")),
                "to": parse_address(headers.get("To")),
                "subject": headers.get("Subject"),
                "date": headers.get("Date"),
                "labelIds": label_ids
            },
            occurred_at=occurred_at
        )

    def _create_message_deleted_event(self, msg_id: str, thread_id: str, history_id: str) -> NewEvent:
        return NewEvent(
            event_id=f"{msg_id}-deleted",
            event_type="gmail.message_deleted",
            entity_id=msg_id,
            data={
                "threadId": thread_id,
                "messageId": msg_id,
            }
        )

    def _create_label_event(self, event_type: str, msg_id: str, thread_id: str, history_id: str, changed_labels: list, all_labels: list) -> NewEvent:
        suffix = "lab-add" if "added" in event_type else "lab-rem"
        return NewEvent(
            event_id=f"{msg_id}-{history_id}-{suffix}",
            event_type=event_type,
            entity_id=msg_id,
            data={
                "threadId": thread_id,
                "messageId": msg_id,
                "labelIds": changed_labels,
                "allLabelIds": all_labels
            }
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
                        pageToken=page_token
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
                    history_id = record.get('id')

                    # 1. Messages Added
                    for item in record.get('messagesAdded', []):
                        msg_ref = item.get('message', {})
                        msg_id = msg_ref.get('id')
                        if not msg_id: continue
                        
                        # Early filter if labels present
                        label_ids = msg_ref.get('labelIds', [])
                        if any(l in self.config.exclude_label_ids for l in label_ids):
                            continue

                        try:
                            msg = self._get_message_metadata(service, msg_id)
                        except HttpError as e:
                            if e.resp.status == 404:
                                logger.warning(f"Message {msg_id} not found in {self.name}, skipping")
                                continue
                            raise
                        # Re-check labels after metadata fetch
                        label_ids = msg.get('labelIds', [])
                        if any(l in self.config.exclude_label_ids for l in label_ids):
                            continue
                        events.append(self._create_message_event(msg_id, msg))

                    # 2. Messages Deleted
                    for item in record.get('messagesDeleted', []):
                        msg_ref = item.get('message', {})
                        msg_id = msg_ref.get('id')
                        if not msg_id: continue
                        
                        label_ids = msg_ref.get('labelIds', [])
                        if any(l in self.config.exclude_label_ids for l in label_ids):
                            continue
                        events.append(self._create_message_deleted_event(msg_id, msg_ref.get('threadId'), history_id))

                    # 3. Labels Added
                    for item in record.get('labelsAdded', []):
                        msg_ref = item.get('message', {})
                        msg_id = msg_ref.get('id')
                        if not msg_id: continue
                        
                        label_ids = msg_ref.get('labelIds', [])
                        if any(l in self.config.exclude_label_ids for l in label_ids):
                            continue
                        events.append(self._create_label_event(
                            "gmail.label_added", msg_id, msg_ref.get('threadId'),
                            history_id, item.get('labelIds', []), label_ids
                        ))

                    # 4. Labels Removed
                    for item in record.get('labelsRemoved', []):
                        msg_ref = item.get('message', {})
                        msg_id = msg_ref.get('id')
                        if not msg_id: continue
                        
                        label_ids = msg_ref.get('labelIds', [])
                        if any(l in self.config.exclude_label_ids for l in label_ids):
                            continue
                        events.append(self._create_label_event(
                            "gmail.label_removed", msg_id, msg_ref.get('threadId'),
                            history_id, item.get('labelIds', []), label_ids
                        ))

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
