import asyncio
import json
import logging
import math
from datetime import datetime, timezone
import websockets
from websockets.exceptions import ConnectionClosed

from src.config import HomeAssistantSourceConfig
from src.schemas import NewEvent
from src.services import AppServices

logger = logging.getLogger(__name__)

class HomeAssistantSource:
    def __init__(self, name: str, config: HomeAssistantSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.ws_url = config.url
        self.access_token = config.access_token
        self.entity_ids = config.entity_ids
        self.message_id = 1
        self.health = services.health.reporter(name)

    def _changed(self, old: dict, new: dict, attr: str = None) -> bool:
        if attr is None:
            old_val = old.get("state") if old else None
            new_val = new.get("state") if new else None
            return old_val != new_val

        old_attrs = old.get("attributes", {}) if old else {}
        new_attrs = new.get("attributes", {}) if new else {}
        return old_attrs.get(attr) != new_attrs.get(attr)

    def _calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate the Haversine distance between two points in meters."""
        if lat1 == lat2 and lon1 == lon2:
            return 0.0

        R = 6371000  # Radius of the earth in meters
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)

        a = math.sin(dphi / 2)**2 + \
            math.cos(phi1) * math.cos(phi2) * \
            math.sin(dlambda / 2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        return R * c

    def _should_filter_location(self, old_state: dict, new_state: dict) -> bool:
        """Check if the location update should be filtered based on threshold."""
        if self.config.location_threshold_meters <= 0:
            return False

        old_attr = old_state.get("attributes") or {}
        new_attr = new_state.get("attributes") or {}

        # For geocoded location, coordinates might be in 'location' attribute as [lat, lon]
        old_loc = old_attr.get("location")
        new_loc = new_attr.get("location")

        if isinstance(old_loc, list) and len(old_loc) == 2 and isinstance(new_loc, list) and len(new_loc) == 2:
            dist = self._calculate_distance(old_loc[0], old_loc[1], new_loc[0], new_loc[1])
            return dist < self.config.location_threshold_meters

        # For device_tracker, coordinates are in 'latitude' and 'longitude' attributes
        old_lat = old_attr.get("latitude")
        old_lon = old_attr.get("longitude")
        new_lat = new_attr.get("latitude")
        new_lon = new_attr.get("longitude")

        if all(v is not None for v in [old_lat, old_lon, new_lat, new_lon]):
            dist = self._calculate_distance(old_lat, old_lon, new_lat, new_lon)
            return dist < self.config.location_threshold_meters

        return False

    def _summarize_location_update(self, trigger: dict) -> dict:
        old_state = trigger.get("from_state") or {}
        new_state = trigger.get("to_state") or {}

        old_attr = old_state.get("attributes") or {}
        new_attr = new_state.get("attributes") or {}

        state_changed = old_state.get("state") != new_state.get("state")
        lat_changed = old_attr.get("latitude") != new_attr.get("latitude")
        lon_changed = old_attr.get("longitude") != new_attr.get("longitude")
        acc_changed = old_attr.get("gps_accuracy") != new_attr.get("gps_accuracy")

        return {
            "kind": "zone_update",
            "entity_id": trigger.get("entity_id"),
            "zone_change": state_changed,
            "gps_change": lat_changed or lon_changed,
            "gps_acc_change": acc_changed,
            "zone": {
                "old": old_state.get("state"),
                "new": new_state.get("state"),
            },
            "gps": {
                "lat": new_attr.get("latitude"),
                "lon": new_attr.get("longitude"),
                "acc": new_attr.get("gps_accuracy"),
            },
            "updated_at": new_state.get("last_updated"),
        }

    def _summarize_geocoded_location_update(self, trigger: dict) -> dict:
        old_state = trigger.get("from_state") or {}
        old_attr = old_state.get("attributes") or {}
        new_state = trigger.get("to_state") or {}
        new_attr = new_state.get("attributes") or {}

        return {
            "kind": "geocoded_location_update",
            "entity_id": trigger.get("entity_id"),
            "addr": {
                "old": old_state.get("state"),
                "new": new_state.get("state"),
            },
            "gps": {
                "old": old_attr.get("location"),
                "new": new_attr.get("location"),
                "acc": new_attr.get("gps_accuracy"),
            },
            "updated_at": new_state.get("last_updated"),
        }

    def _summarize_next_alarm_changed(self, trigger: dict) -> dict:
        old_state = trigger.get("from_state") or {}
        new_state = trigger.get("to_state") or {}

        return {
            "kind": "next_alarm_changed",
            "entity_id": trigger.get("entity_id"),
            "alarm_utc": {
                "old": old_state.get("state"),
                "new": new_state.get("state"),
            },
            "updated_at": new_state.get("last_updated"),
        }

    def _summarize_generic_sensor_update(self, trigger: dict) -> dict:
        old_state = trigger.get("from_state") or {}
        new_state = trigger.get("to_state") or {}

        return {
            "kind": "generic_sensor_update",
            "entity_id": trigger.get("entity_id"),
            "state": {
                "old": old_state.get("state"),
                "new": new_state.get("state"),
            },
            "updated_at": new_state.get("last_updated"),
        }

    def _new_state_value(self, update: dict):
        for key in ("zone", "addr", "alarm_utc", "state"):
            value = update.get(key)
            if isinstance(value, dict):
                return value.get("new")
        return None

    async def run(self):
        """Main loop with reconnection logic."""
        while True:
            self.health.checking()
            try:
                await self._listen()
            except ConnectionClosed:
                logger.warning(f"Connection to Home Assistant {self.name} closed. Reconnecting in 10s...")
                self.health.unhealthy(
                    "connectivity",
                    "The Home Assistant WebSocket connection closed.",
                )
                await asyncio.sleep(10)
            except PermissionError:
                logger.error(f"Authentication failed for Home Assistant source {self.name}")
                self.health.unhealthy(
                    "authentication",
                    "Home Assistant rejected the configured access token.",
                    action="Create and configure a valid long-lived Home Assistant access token.",
                )
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Error in Home Assistant source {self.name}: {e}. Retrying in 30s...", exc_info=True)
                self.health.unhealthy_from_exception(e)
                await asyncio.sleep(30)

    async def _listen(self):
        self.message_id = 1
        async with websockets.connect(self.ws_url) as ws:
            # 1) Receive auth_required
            msg = json.loads(await ws.recv())
            if msg["type"] != "auth_required":
                logger.error(f"Unexpected initial message from {self.name}: {msg}")
                raise RuntimeError("Home Assistant did not start the expected authentication flow")

            # 2) Authenticate
            await ws.send(json.dumps({
                "type": "auth",
                "access_token": self.access_token
            }))

            msg = json.loads(await ws.recv())
            if msg["type"] != "auth_ok":
                logger.error(f"Authentication failed for {self.name}: {msg}")
                raise PermissionError("Home Assistant authentication failed")

            logger.info(f"Authenticated with Home Assistant {self.name}")

            # 3) Subscribe to entities
            await ws.send(json.dumps({
                "id": self.message_id,
                "type": "subscribe_trigger",
                "trigger": {
                    "platform": "state",
                    "entity_id": sorted(self.entity_ids),
                }
            }))
            self.message_id += 1

            ack = json.loads(await ws.recv())
            if ack.get("type") != "result" or not ack.get("success"):
                logger.error(f"Subscription failed for {self.name}: {ack}")
                raise RuntimeError("Home Assistant rejected the entity subscription")

            logger.info(f"Subscribed to {len(self.entity_ids)} entities in Home Assistant {self.name}")
            self.health.healthy("Authenticated and subscribed to Home Assistant entity changes.")

            while True:
                raw = json.loads(await ws.recv())

                if raw.get("type") != "event":
                    continue

                event = raw.get("event") or {}
                variables = event.get("variables") or {}
                trigger = variables.get("trigger") or {}

                entity_id = trigger.get("entity_id", "")
                if entity_id not in self.entity_ids:
                    continue

                if entity_id.startswith("device_tracker."):
                    update = self._summarize_location_update(trigger)
                    if not update["zone_change"]:
                        # If zone hasn't changed, we don't emit zone update events
                        continue
                    event_type = "home_assistant.zone_update"
                elif entity_id.endswith("_geocoded_location"):
                    update = self._summarize_geocoded_location_update(trigger)
                    addr = update.get("addr") or {}
                    if addr.get("old") == addr.get("new"):
                        continue
                    if self._should_filter_location(trigger.get("from_state") or {}, trigger.get("to_state") or {}):
                        logger.debug(f"Filtering geocoded location update for {entity_id} - below threshold")
                        continue
                    event_type = "home_assistant.geocoded_location_update"
                elif entity_id.endswith("_next_alarm"):
                    update = self._summarize_next_alarm_changed(trigger)
                    alarm_utc = update.get("alarm_utc") or {}
                    if alarm_utc.get("old") == alarm_utc.get("new"):
                        continue
                    event_type = "home_assistant.next_alarm_changed"
                elif entity_id.startswith("sensor."):
                    update = self._summarize_generic_sensor_update(trigger)
                    if not self._changed(trigger.get("from_state"), trigger.get("to_state")):
                        continue
                    event_type = "home_assistant.generic_sensor_update"
                else:
                    # Fallback
                    update = self._summarize_generic_sensor_update(trigger)
                    if not self._changed(trigger.get("from_state"), trigger.get("to_state")):
                        continue
                    event_type = "home_assistant.entity_update"

                # Check for junk states
                new_state_value = self._new_state_value(update)
                if new_state_value in ("unknown", "unavailable"):
                    logger.debug(f"Ignoring junk state '{new_state_value}' for {entity_id}")
                    continue

                updated_at = update.get("updated_at")
                if not updated_at:
                    continue

                event_id = f"{entity_id}-{updated_at}"

                new_event = NewEvent(
                    event_id=event_id,
                    event_type=event_type,
                    entity_id=entity_id,
                    data=update,
                    occurred_at=datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                )

                self.services.writer.write_events(self.source_id, [new_event])
                logger.info(f"Published event {event_id} from {self.name}")
