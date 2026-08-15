import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from src.config import GoogleHealthSourceConfig
from src.schemas import NewEvent
from src.services import AppServices
from src.utils.google_auth import get_google_credentials

logger = logging.getLogger(__name__)

BASE_URL = "https://health.googleapis.com/v4"

# Data types that use interval-based time filtering (session/interval types)
INTERVAL_DATA_TYPES = {"steps", "floors", "distance", "altitude", "active-zone-minutes",
                       "active-minutes", "time-in-heart-rate-zone", "sedentary-period",
                       "daily-heart-rate-zones", "hydration-log"}

# Data types that use session interval filtering
SESSION_DATA_TYPES = {"exercise", "sleep"}

# Data types that use sample_time filtering
SAMPLE_DATA_TYPES = {"weight", "body-fat", "heart-rate", "heart-rate-variability",
                     "oxygen-saturation", "respiratory-rate-sleep-summary"}

# Data types that use daily summary date filtering
DAILY_DATA_TYPES = {"daily-resting-heart-rate", "daily-heart-rate-variability",
                    "daily-sleep-temperature-derivations", "daily-oxygen-saturation",
                    "daily-vo2-max", "daily-respiratory-rate", "run-vo2-max", "vo2-max"}


def _build_filter_string(data_type: str, start_time: datetime) -> Optional[str]:
    """Build the appropriate filter string for a given data type and start time."""
    # Convert camelCase field name to kebab-case data type for filter pattern
    # The filter uses the camelCase version of the data type
    camel = data_type.replace("-", " ").title().replace(" ", "")
    camel = camel[0].lower() + camel[1:]

    if data_type in INTERVAL_DATA_TYPES:
        ts = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f'{camel}.interval.start_time >= "{ts}"'
    elif data_type == "sleep":
        ts = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f'sleep.interval.end_time >= "{ts}"'
    elif data_type == "exercise":
        date_str = start_time.strftime("%Y-%m-%d")
        return f'exercise.interval.civil_start_time >= "{date_str}"'
    elif data_type in SAMPLE_DATA_TYPES:
        ts = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f'{camel}.sample_time.physical_time >= "{ts}"'
    elif data_type in DAILY_DATA_TYPES:
        date_str = start_time.strftime("%Y-%m-%d")
        return f'{camel}.date >= "{date_str}"'
    return None


def _extract_timestamp(data_type: str, data_point: Dict[str, Any]) -> Optional[datetime]:
    """Extract the most relevant timestamp from a data point."""
    # Try interval-based timestamps
    for field_name in data_point:
        field_val = data_point[field_name]
        if isinstance(field_val, dict):
            interval = field_val.get("interval")
            if interval:
                start = interval.get("startTime")
                if start:
                    return _parse_rfc3339(start)

            sample_time = field_val.get("sampleTime")
            if sample_time:
                physical = sample_time.get("physicalTime")
                if physical:
                    return _parse_rfc3339(physical)

            create_time = field_val.get("createTime")
            if create_time:
                return _parse_rfc3339(create_time)

    return None


def _parse_rfc3339(value: str) -> Optional[datetime]:
    """Parse an RFC 3339 timestamp string."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _data_point_id(data_type: str, data_point: Dict[str, Any]) -> str:
    """Generate a stable event ID for a data point."""
    name = data_point.get("name", "")
    if name:
        return f"ghealth_{data_type}_{name.split('/')[-1]}"

    # Fallback: hash the data point content
    content = f"{data_type}:{data_point}"
    digest = hashlib.sha256(content.encode()).hexdigest()[:16]
    return f"ghealth_{data_type}_{digest}"


def _extract_entity_id(data_point: Dict[str, Any]) -> Optional[str]:
    """Extract entity ID from data point name."""
    name = data_point.get("name", "")
    if name:
        return name.split("/")[-1]
    return None


class GoogleHealthSource:
    """
    Source for Google Health API (v4).
    Polls health and fitness data points (steps, sleep, exercise, weight, etc.)
    and emits them as events.
    """

    def __init__(
        self,
        name: str,
        config: GoogleHealthSourceConfig,
        services: AppServices,
        source_id: int,
    ):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.cursor_manager = services.cursor
        self.writer = services.writer
        self.health = services.health.reporter(name)
        self._poll_errors: List[Exception] = []

    def _get_auth_headers(self) -> Dict[str, str]:
        """Get authorization headers using Google OAuth2 credentials."""
        creds = get_google_credentials(self.config.token_file, self.name)
        return {"Authorization": f"Bearer {creds.token}"}

    async def run(self) -> None:
        """Main polling loop."""
        logger.info(
            f"Starting Google Health source '{self.name}' with poll interval "
            f"{self.config.poll_interval}s, data types: {self.config.data_types}"
        )
        while True:
            self.health.checking()
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(f"Error in Google Health source '{self.name}' poll loop")
                self.health.unhealthy_from_exception(error)
            else:
                if self._poll_errors:
                    self.health.unhealthy_from_exception(self._poll_errors[0])
                else:
                    self.health.healthy()
            await asyncio.sleep(self.config.poll_interval)

    async def poll(self) -> None:
        """Perform a single poll across all configured data types."""
        self._poll_errors = []
        now = datetime.now(timezone.utc)

        last_cursor_str = self.cursor_manager.get_last_cursor(self.source_id)
        if last_cursor_str:
            try:
                start_time = datetime.fromisoformat(last_cursor_str)
            except ValueError:
                logger.error(f"Invalid cursor for Google Health source '{self.name}': {last_cursor_str}")
                start_time = now - timedelta(days=self.config.lookback_days)
        else:
            start_time = now - timedelta(days=self.config.lookback_days)

        headers = self._get_auth_headers()
        all_events: List[NewEvent] = []
        failures: List[Exception] = []

        async with httpx.AsyncClient() as client:
            for data_type in self.config.data_types:
                try:
                    events = await self._fetch_data_type(client, headers, data_type, start_time)
                    all_events.extend(events)
                except httpx.HTTPStatusError as e:
                    logger.error(
                        f"Google Health source '{self.name}' HTTP error for "
                        f"'{data_type}': {e.response.status_code} {e.response.text}"
                    )
                    failures.append(e)
                except Exception as error:
                    logger.exception(
                        f"Google Health source '{self.name}' error fetching '{data_type}'"
                    )
                    failures.append(error)

        if all_events:
            self.writer.write_events(self.source_id, all_events)
            logger.info(f"Google Health source '{self.name}' wrote {len(all_events)} events")

        self._poll_errors = failures
        if failures:
            return

        self.cursor_manager.set_cursor(self.source_id, now.isoformat())

    async def _fetch_data_type(
        self,
        client: httpx.AsyncClient,
        headers: Dict[str, str],
        data_type: str,
        start_time: datetime,
    ) -> List[NewEvent]:
        """Fetch all pages of data points for a single data type."""
        events: List[NewEvent] = []
        page_token: Optional[str] = None

        while True:
            params: Dict[str, Any] = {"pageSize": 1000}
            filter_str = _build_filter_string(data_type, start_time)
            if filter_str:
                params["filter"] = filter_str
            if page_token:
                params["pageToken"] = page_token

            url = f"{BASE_URL}/users/me/dataTypes/{data_type}/dataPoints"
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            body = response.json()

            for dp in body.get("dataPoints", []):
                event = self._map_data_point(data_type, dp)
                events.append(event)

            page_token = body.get("nextPageToken")
            if not page_token:
                break

        logger.debug(f"Google Health source '{self.name}' fetched {len(events)} '{data_type}' data points")
        return events

    def _map_data_point(self, data_type: str, data_point: Dict[str, Any]) -> NewEvent:
        """Convert a raw API data point into a NewEvent."""
        event_id = _data_point_id(data_type, data_point)
        entity_id = _extract_entity_id(data_point)
        occurred_at = _extract_timestamp(data_type, data_point)

        # Build a clean event type: google.health.<data_type_underscored>
        event_type_suffix = data_type.replace("-", "_")
        event_type = f"google.health.{event_type_suffix}"

        # Remove the name field from data payload (already used for IDs)
        payload = {k: v for k, v in data_point.items() if k != "name"}

        return NewEvent(
            event_id=event_id,
            event_type=event_type,
            entity_id=entity_id,
            occurred_at=occurred_at,
            data=payload,
        )
