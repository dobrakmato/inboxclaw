"""Strava activity source."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.config import StravaSourceConfig
from src.schemas import NewEvent
from src.services import AppServices

logger = logging.getLogger(__name__)

BASE_URL = "https://www.strava.com"
TOKEN_STATE_KEY = "strava:oauth_token_state"
ACTIVITY_KEY_PREFIX = "strava:activity:"
TOKEN_REFRESH_MARGIN_SECONDS = 3600


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _token_seed_hash(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


class StravaSource:
    """Poll the authenticated athlete's recent activities."""

    def __init__(
        self,
        name: str,
        config: StravaSourceConfig,
        services: AppServices,
        source_id: int,
    ) -> None:
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.writer = services.writer
        self.kv = services.kv
        self.health = services.health.reporter(name)
        self.client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

    async def run(self) -> None:
        logger.info(
            "Starting Strava source '%s' with poll interval %ss",
            self.name,
            self.config.poll_interval,
        )
        try:
            while True:
                self.health.checking()
                try:
                    await self.poll()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.exception("Error in Strava source '%s' poll loop", self.name)
                    self.health.unhealthy_from_exception(error)
                else:
                    self.health.healthy()
                await asyncio.sleep(self.config.poll_interval)
        finally:
            await self.client.aclose()

    async def poll(self) -> None:
        """Fetch the rolling activity window and publish new or changed activities."""
        self._validate_credentials()
        access_token = await self._get_access_token()
        after = datetime.now(timezone.utc) - timedelta(days=self.config.lookback_days)
        activities = await self._fetch_activities(access_token, after)

        # Strava returns newest first. Publishing oldest first keeps a backfill intuitive.
        for activity in reversed(activities):
            await self._process_activity(activity)

    def _validate_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("client_id", self.config.client_id),
                ("client_secret", self.config.client_secret),
                ("refresh_token", self.config.refresh_token),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing Strava configuration: {', '.join(missing)}")

    async def _get_access_token(self) -> str:
        now = int(datetime.now(timezone.utc).timestamp())
        seed_hash = _token_seed_hash(self.config.refresh_token)
        stored = self.kv.get(self.source_id, TOKEN_STATE_KEY)
        if not isinstance(stored, dict) or stored.get("seed_hash") != seed_hash:
            stored = {}

        access_token = stored.get("access_token")
        expires_at = stored.get("expires_at")
        if (
            isinstance(access_token, str)
            and access_token
            and isinstance(expires_at, int)
            and expires_at > now + TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return access_token

        refresh_token = stored.get("refresh_token") or self.config.refresh_token
        response = await self.client.post(
            "/oauth/token",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        body = response.json()

        new_access_token = body.get("access_token")
        new_refresh_token = body.get("refresh_token")
        new_expires_at = body.get("expires_at")
        if not isinstance(new_access_token, str) or not new_access_token:
            raise ValueError("Strava token response did not include an access_token")
        if not isinstance(new_refresh_token, str) or not new_refresh_token:
            raise ValueError("Strava token response did not include a refresh_token")
        if not isinstance(new_expires_at, int):
            raise ValueError("Strava token response did not include an integer expires_at")

        self.kv.set(
            self.source_id,
            TOKEN_STATE_KEY,
            {
                "seed_hash": seed_hash,
                "access_token": new_access_token,
                "refresh_token": new_refresh_token,
                "expires_at": new_expires_at,
            },
        )
        return new_access_token

    async def _fetch_activities(
        self,
        access_token: str,
        after: datetime,
    ) -> list[dict[str, Any]]:
        activities: list[dict[str, Any]] = []
        page = 1
        headers = {"Authorization": f"Bearer {access_token}"}

        while True:
            response = await self.client.get(
                "/api/v3/athlete/activities",
                headers=headers,
                params={
                    "after": int(after.timestamp()),
                    "page": page,
                    "per_page": self.config.per_page,
                },
            )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list):
                raise ValueError("Strava activities response was not a list")

            if any(not isinstance(item, dict) for item in batch):
                raise RuntimeError("Strava activities response contained a malformed activity")
            activities.extend(batch)
            if len(batch) < self.config.per_page:
                break
            page += 1

        logger.debug("Strava source '%s' fetched %s activities", self.name, len(activities))
        return activities

    async def _process_activity(self, activity: dict[str, Any]) -> None:
        activity_id = activity.get("id")
        if not isinstance(activity_id, (int, str)) or isinstance(activity_id, bool):
            raise RuntimeError(f"Strava activity has no valid id: {activity_id!r}")

        entity_id = str(activity_id)
        key = f"{ACTIVITY_KEY_PREFIX}{entity_id}"
        state = self._state_for_comparison(activity)
        fingerprint = self._fingerprint(state)
        cached = self.kv.get(self.source_id, key)

        if not isinstance(cached, dict):
            event = NewEvent(
                event_id=f"strava-{entity_id}-created",
                event_type="strava.activity_created",
                entity_id=entity_id,
                occurred_at=_parse_datetime(activity.get("start_date")),
                data={"activity_id": entity_id, "activity": activity},
            )
        elif cached.get("fingerprint") == fingerprint:
            return
        else:
            previous = cached.get("state") if isinstance(cached.get("state"), dict) else {}
            event = NewEvent(
                event_id=f"strava-{entity_id}-updated-{fingerprint[:16]}",
                event_type="strava.activity_updated",
                entity_id=entity_id,
                occurred_at=_parse_datetime(activity.get("start_date")),
                data={
                    "activity_id": entity_id,
                    "changed_fields": self._diff(previous, state),
                    "activity": activity,
                },
            )

        # Persist the event first. If the write fails, the unchanged cache lets the
        # next poll retry; EventWriter's event_id uniqueness keeps retries idempotent.
        self.writer.write_events(self.source_id, [event])
        self.kv.set(
            self.source_id,
            key,
            {"fingerprint": fingerprint, "state": state},
        )

    def _state_for_comparison(self, activity: dict[str, Any]) -> dict[str, Any]:
        ignored = set(self.config.ignored_fields)
        return {key: value for key, value in activity.items() if key not in ignored}

    @staticmethod
    def _fingerprint(activity: dict[str, Any]) -> str:
        encoded = json.dumps(
            activity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            key: {"before": before.get(key), "after": after.get(key)}
            for key in sorted(before.keys() | after.keys())
            if (key in before) != (key in after) or before.get(key) != after.get(key)
        }
