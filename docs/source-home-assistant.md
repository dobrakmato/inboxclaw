# Home Assistant Source

The Home Assistant source subscribes to entity state changes in a Home Assistant instance via WebSocket. It is designed to track `device_tracker` entities, geocoded locations, alarms, and other sensors in real-time.

Use this source to build location-aware workflows: trigger automations when you arrive at a zone, log travel history, notify others when you leave work, or integrate phone-based presence with other services.

## Getting Started

### 1. Get an API token

1. Open your Home Assistant instance in a browser.
2. Click your profile name at the bottom of the sidebar.
3. Scroll to **Long-Lived Access Tokens**.
4. Click **Create Token**, name it (e.g. "Ingest Pipeline"), and copy the token.

You can provide the token in `config.yaml` or via the `HOME_ASSISTANT_TOKEN` environment variable.

### 2. Find entity IDs

Go to **Settings** → **Devices & Services** → **Entities** and search for `device_tracker`. Copy the Entity ID (e.g. `device_tracker.iphone_15_pro`).

### 3. Add the source to `config.yaml`

```yaml
sources:
  my_home:
    type: home_assistant
    url: "wss://YOUR_HA_HOST:8123/api/websocket"
    entity_ids:
      - "device_tracker.my_phone"
```

### 4. (Recommended) Enable precise location

For best results with coordinate tracking, configure your Home Assistant Companion app for exact location:

- **Android**: Settings → Companion App → Manage Sensors → Location Sensors → Enable "Background location" and "Single accurate location".
- **iOS**: Settings → Companion App → Location → Set to "Always" and enable "Precise Location".

## Core Concepts

### WebSocket Connection

Unlike polling-based sources, this source maintains a persistent WebSocket connection to Home Assistant. Events are delivered in near real-time when an entity's state or attributes change.

### State Triggers

The source uses Home Assistant's `subscribe_trigger` API with a `state` platform. Home Assistant handles the filtering and only sends updates for the specific entities you've configured.

### Event Classification

Updates are automatically classified based on the entity type:

- **Zone updates** (`device_tracker.*`): Triggered when the zone state changes (e.g. `home` → `not_home`). Coordinate-only changes within the same zone are ignored.
- **Geocoded location** (`sensor.*_geocoded_location`): Triggered when the human-readable address changes.
- **Alarm changes** (`sensor.*_next_alarm`): Triggered when the next scheduled alarm changes.
- **Generic sensors** (any other `sensor.*`): Triggered when the sensor state changes.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_home:
    type: home_assistant
    url: "wss://ha.example.com/api/websocket"
    entity_ids:
      - "device_tracker.my_phone"
```

### Full Configuration

```yaml
sources:
  family_tracking:
    type: home_assistant
    url: "wss://ha.example.com/api/websocket"
    access_token: "YOUR_LONG_LIVED_ACCESS_TOKEN"
    entity_ids:
      - "device_tracker.phone_1"
      - "device_tracker.phone_2"
      - "device_tracker.tablet_1"
```

### Configuration Reference

| Parameter      | Type     | Default  | Description                                                                          |
|:---------------|:---------|:---------|:-------------------------------------------------------------------------------------|
| `url`          | `string` | Required | Home Assistant WebSocket URL (e.g. `wss://ha.example.com/api/websocket`).            |
| `access_token` | `string` | Env var  | Long-Lived Access Token. Defaults to `HOME_ASSISTANT_TOKEN` environment variable.    |
| `entity_ids`   | `list`   | Required | List of entity IDs to track (e.g. `device_tracker.my_phone`).                        |

## Event Definitions

| Type                                       | Entity ID                        | Description                                                  |
|:-------------------------------------------|:---------------------------------|:-------------------------------------------------------------|
| `home_assistant.zone_update`               | `device_tracker.<name>`          | Zone state changed (e.g. `home` → `not_home`).               |
| `home_assistant.geocoded_location_update`  | `sensor.*_geocoded_location`     | Human-readable address changed.                              |
| `home_assistant.next_alarm_changed`        | `sensor.*_next_alarm`            | Next scheduled alarm changed.                                |
| `home_assistant.generic_sensor_update`     | `sensor.*`                       | Any other tracked sensor state changed.                      |

### Event Examples

#### `home_assistant.zone_update`

```json
{
  "id": 1,
  "event_id": "ha-zone-device_tracker.my_phone-1710500000",
  "event_type": "home_assistant.zone_update",
  "entity_id": "device_tracker.my_phone",
  "created_at": "2024-03-15T14:00:00+00:00",
  "data": {
    "entity_id": "device_tracker.my_phone",
    "state_changed": true,
    "coords_changed": false,
    "gps_accuracy_changed": false,
    "old_state": "not_home",
    "new_state": "home",
    "latitude": 51.5074,
    "longitude": -0.1278,
    "gps_accuracy": 15,
    "source": "gps",
    "last_updated": "2024-03-15T14:00:00.000000+00:00"
  },
  "meta": {}
}
```

- `state_changed`: Always `true` for zone updates (coordinate-only changes are filtered out).
- `coords_changed` / `gps_accuracy_changed`: Whether coordinates or GPS accuracy also changed alongside the zone.

#### `home_assistant.geocoded_location_update`

```json
{
  "id": 2,
  "event_id": "ha-geo-sensor.my_phone_geocoded_location-1710500000",
  "event_type": "home_assistant.geocoded_location_update",
  "entity_id": "sensor.my_phone_geocoded_location",
  "created_at": "2024-03-15T14:00:00+00:00",
  "data": {
    "kind": "geocoded_location_update",
    "entity_id": "sensor.my_phone_geocoded_location",
    "label_changed": true,
    "state": "123 Main St, New York, NY",
    "location": [40.7128, -74.0060],
    "country": "United States",
    "last_updated": "2024-03-15T14:00:00.000000+00:00"
  },
  "meta": {}
}
```

#### `home_assistant.next_alarm_changed`

```json
{
  "id": 3,
  "event_id": "ha-alarm-sensor.my_phone_next_alarm-1710500000",
  "event_type": "home_assistant.next_alarm_changed",
  "entity_id": "sensor.my_phone_next_alarm",
  "created_at": "2024-03-15T14:00:00+00:00",
  "data": {
    "kind": "next_alarm_changed",
    "entity_id": "sensor.my_phone_next_alarm",
    "changed": true,
    "old_alarm_utc": "2024-03-16T06:00:00.000000+00:00",
    "new_alarm_utc": "2024-03-16T07:00:00.000000+00:00",
    "local_time": "08:00:00",
    "package": "com.android.deskclock",
    "last_updated": "2024-03-15T14:00:00.000000+00:00"
  },
  "meta": {}
}
```

#### `home_assistant.generic_sensor_update`

```json
{
  "id": 4,
  "event_id": "ha-sensor-sensor.my_phone_battery_level-1710500000",
  "event_type": "home_assistant.generic_sensor_update",
  "entity_id": "sensor.my_phone_battery_level",
  "created_at": "2024-03-15T14:00:00+00:00",
  "data": {
    "kind": "generic_sensor_update",
    "entity_id": "sensor.my_phone_battery_level",
    "old_state": "85",
    "new_state": "84",
    "last_updated": "2024-03-15T14:00:00.000000+00:00"
  },
  "meta": {}
}
```
