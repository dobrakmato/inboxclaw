# Home Assistant Source

The Home Assistant source allows the ingest pipeline to subscribe to entity changes in a Home Assistant instance using WebSockets. It is specifically designed to track `device_tracker` entities, geocoded locations, alarms, and other sensors.

## Why use this source?

This source is ideal for building location-aware workflows, such as:
- Triggering home automation when you arrive at a specific zone.
- Logging travel history or commute times.
- Notifying others when you leave work or arrive home.
- Integrating phone-based presence with other services.

## Core Concepts

### WebSocket Connection
Unlike polling-based sources, this source maintains a persistent WebSocket connection to Home Assistant. This allows for near-instantaneous event delivery when an entity's state or attributes change.

### State Triggers
The source uses Home Assistant's `subscribe_trigger` API with a `state` platform. This means Home Assistant handles the filtering and only sends updates for the specific entities you've configured.

### Event Classification
The source automatically classifies updates into several types:
- **Zone Updates**: Triggered when the entity's state changes (e.g., `home` to `not_home`). Coordinate-only changes within the same zone are ignored.
- **Geocoded Location Updates**: Triggered when the human-readable address change (e.g. from `sensor.*_geocoded_location`).
- **Alarm Changes**: Triggered when the next scheduled alarm on a device changes (e.g. from `sensor.*_next_alarm`).
- **Sensor Updates**: Triggered when any other tracked sensor state changes.

## Configuration

### Minimal Configuration

To get started, you need your Home Assistant WebSocket URL, a Long-Lived Access Token, and at least one entity ID to track. You can also provide the token via the `HOME_ASSISTANT_TOKEN` environment variable.

```yaml
sources:
  my_home:
    type: home_assistant
    url: "wss://YOUR_HA_HOST:8123/api/websocket"
    # access_token: "YOUR_LONG_LIVED_ACCESS_TOKEN" # Optional if HOME_ASSISTANT_TOKEN is set
    entity_ids:
      - "device_tracker.my_phone"
```

### Full Configuration

You can track multiple devices from the same Home Assistant instance. If you prefer to set the access token in the configuration file, you can do so as shown below:

```yaml
sources:
  family_tracking:
    type: home_assistant
    url: "wss://ha.example.com/api/websocket"
    access_token: "..."
    entity_ids:
      - "device_tracker.phone_1"
      - "device_tracker.phone_2"
      - "device_tracker.tablet_1"
```

## Setup Guide

### 1. Get an API Token
1. Open your Home Assistant instance in a web browser.
2. Click on your profile name at the bottom of the sidebar.
3. Scroll down to the **Long-Lived Access Tokens** section.
4. Click **Create Token**, give it a name (e.g., "Ingest Pipeline"), and copy the token.

### 2. Find Entity IDs
1. Go to **Settings** -> **Devices & Services** -> **Entities**.
2. Search for `device_tracker`.
3. Copy the **Entity ID** (e.g., `device_tracker.iphone_15_pro`).

### 3. Ensure "Exact" Location Mode
For the best results with coordinate tracking, ensure your Home Assistant Companion app is configured for "Exact" location tracking.
- **Android**: Settings -> Companion App -> Manage Sensors -> Location Sensors -> Enable "Background location" and "Single accurate location".
- **iOS**: Settings -> Companion App -> Location -> Set to "Always" and enable "Precise Location".

## Event Definitions

| Type | Entity ID | Description |
| :--- | :--- | :--- |
| `home_assistant.zone_update` | `device_tracker.<name>` | Triggered when the zone state (e.g., `home`, `office`) changes. Coordinate-only changes within a zone are ignored. |
| `home_assistant.geocoded_location_update` | `sensor.*_geocoded_location` | Triggered when the geocoded address changes. |
| `home_assistant.next_alarm_changed` | `sensor.*_next_alarm` | Triggered when the next scheduled alarm changes. |
| `home_assistant.generic_sensor_update` | `sensor.*` | Triggered when a generic sensor state changes. |

### Data Payloads

#### Zone Updates

The `data` field contains:

```json
{
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
}
```

- `state_changed`: Always `true` for zone updates (as coordinate-only changes are filtered).
- `coords_changed`: `true` if `latitude` or `longitude` changed alongside the zone change.
- `gps_accuracy_changed`: `true` if `gps_accuracy` changed alongside the zone change.
- `last_updated`: The timestamp from Home Assistant when the change occurred.

#### Geocoded Location Updates

```json
{
  "kind": "geocoded_location_update",
  "entity_id": "sensor.my_phone_geocoded_location",
  "label_changed": true,
  "state": "123 Main St, New York, NY",
  "location": [40.7128, -74.0060],
  "country": "United States",
  "last_updated": "2024-03-15T14:00:00.000000+00:00"
}
```

#### Next Alarm Changed

```json
{
  "kind": "next_alarm_changed",
  "entity_id": "sensor.my_phone_next_alarm",
  "changed": true,
  "old_alarm_utc": "2024-03-16T06:00:00.000000+00:00",
  "new_alarm_utc": "2024-03-16T07:00:00.000000+00:00",
  "local_time": "08:00:00",
  "package": "com.android.deskclock",
  "last_updated": "2024-03-15T14:00:00.000000+00:00"
}
```

#### Generic Sensor Update

```json
{
  "kind": "generic_sensor_update",
  "entity_id": "sensor.my_phone_battery_level",
  "old_state": "85",
  "new_state": "84",
  "last_updated": "2024-03-15T14:00:00.000000+00:00"
}
```
