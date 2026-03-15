import asyncio
import json
import logging
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

    def _changed(self, old: dict, new: dict, attr: str = None) -> bool:
        if attr is None:
            old_val = old.get("state") if old else None
            new_val = new.get("state") if new else None
            return old_val != new_val

        old_attrs = old.get("attributes", {}) if old else {}
        new_attrs = new.get("attributes", {}) if new else {}
        return old_attrs.get(attr) != new_attrs.get(attr)

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
            "entity_id": trigger.get("entity_id"),
            "state_changed": state_changed,
            "coords_changed": lat_changed or lon_changed,
            "gps_accuracy_changed": acc_changed,
            "old_state": old_state.get("state"),
            "new_state": new_state.get("state"),
            "latitude": new_attr.get("latitude"),
            "longitude": new_attr.get("longitude"),
            "gps_accuracy": new_attr.get("gps_accuracy"),
            "source": new_attr.get("source"),
            "last_updated": new_state.get("last_updated"),
        }

    def _summarize_geocoded_location_update(self, trigger: dict) -> dict:
        old_state = trigger.get("from_state") or {}
        new_state = trigger.get("to_state") or {}
        new_attr = new_state.get("attributes") or {}

        return {
            "kind": "geocoded_location_update",
            "entity_id": trigger.get("entity_id"),
            "label_changed": self._changed(old_state, new_state),
            "state": new_state.get("state"),
            "location": new_attr.get("location"),
            "name": new_attr.get("name"),
            "country": new_attr.get("country"),
            "administrative_area": new_attr.get("administrative_area"),
            "locality": new_attr.get("locality"),
            "sub_locality": new_attr.get("sub_locality"),
            "thoroughfare": new_attr.get("thoroughfare"),
            "postal_code": new_attr.get("postal_code"),
            "last_updated": new_state.get("last_updated"),
        }

    def _summarize_next_alarm_changed(self, trigger: dict) -> dict:
        old_state = trigger.get("from_state") or {}
        new_state = trigger.get("to_state") or {}
        new_attr = new_state.get("attributes") or {}

        return {
            "kind": "next_alarm_changed",
            "entity_id": trigger.get("entity_id"),
            "changed": self._changed(old_state, new_state),
            "old_alarm_utc": old_state.get("state"),
            "new_alarm_utc": new_state.get("state"),
            "local_time": new_attr.get("local_time"),
            "package": new_attr.get("package"),
            "time_in_milliseconds": new_attr.get("time_in_milliseconds"),
            "last_updated": new_state.get("last_updated"),
        }

    def _summarize_generic_sensor_update(self, trigger: dict) -> dict:
        old_state = trigger.get("from_state") or {}
        new_state = trigger.get("to_state") or {}

        return {
            "kind": "generic_sensor_update",
            "entity_id": trigger.get("entity_id"),
            "old_state": old_state.get("state"),
            "new_state": new_state.get("state"),
            "last_updated": new_state.get("last_updated"),
        }

    async def run(self):
        """Main loop with reconnection logic."""
        while True:
            try:
                await self._listen()
            except ConnectionClosed:
                logger.warning(f"Connection to Home Assistant {self.name} closed. Reconnecting in 10s...")
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"Error in Home Assistant source {self.name}: {e}. Retrying in 30s...", exc_info=True)
                await asyncio.sleep(30)

    async def _listen(self):
        async with websockets.connect(self.ws_url) as ws:
            # 1) Receive auth_required
            msg = json.loads(await ws.recv())
            if msg["type"] != "auth_required":
                logger.error(f"Unexpected initial message from {self.name}: {msg}")
                return

            # 2) Authenticate
            await ws.send(json.dumps({
                "type": "auth",
                "access_token": self.access_token
            }))

            msg = json.loads(await ws.recv())
            if msg["type"] != "auth_ok":
                logger.error(f"Authentication failed for {self.name}: {msg}")
                return

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
                return

            logger.info(f"Subscribed to {len(self.entity_ids)} entities in Home Assistant {self.name}")

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
                    if not update["state_changed"]:
                        continue
                    event_type = "home_assistant.zone_update"
                elif entity_id.endswith("_geocoded_location"):
                    update = self._summarize_geocoded_location_update(trigger)
                    # We report any label change for geocoded location
                    if not update["label_changed"]:
                        continue
                    event_type = "home_assistant.geocoded_location_update"
                elif entity_id.endswith("_next_alarm"):
                    update = self._summarize_next_alarm_changed(trigger)
                    if not update["changed"]:
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
                if update.get("new_state") in ("unknown", "unavailable"):
                    logger.debug(f"Ignoring junk state '{update['new_state']}' for {entity_id}")
                    continue

                last_updated = update.get("last_updated")
                if not last_updated:
                    continue
                
                event_id = f"{entity_id}-{last_updated}"
                
                new_event = NewEvent(
                    event_id=event_id,
                    event_type=event_type,
                    entity_id=entity_id,
                    data=update,
                    occurred_at=datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                )

                self.services.writer.write_events(self.source_id, [new_event])
                logger.info(f"Published event {event_id} from {self.name}")
