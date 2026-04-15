# Google Health

The **Google Health** source connects to the [Google Health API (v4)](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints) to pull health and fitness data from Fitbit and other Google-connected devices. It polls for data points like steps, sleep sessions, exercises, weight measurements, heart rate, and more — then converts each data point into a structured event.

Use this source if you want to track your health metrics alongside other life events in Inboxclaw.

## Prerequisites

- A Google Cloud project with the **Health API** enabled.
- OAuth2 credentials (token file) with the required health scopes. Run the auth CLI with `--scopes "health"` to request all three:
  - `health_activity` — steps, exercise, distance, floors, active minutes
  - `health_sleep` — sleep sessions
  - `health_metrics` — heart rate, weight, body fat, SpO2, respiratory rate

  See [Google Auth CLI](google-auth-cli.md) for setup instructions.

  ```bash
  inboxclaw google auth \
    --credentials-file data/credentials.json \
    --token "data/google_token.json" \
    --scopes "health"
  ```

## Minimal Configuration

```yaml
sources:
  google_health:
    token_file: "data/google_token.json"
```

This uses the defaults: polls every 10 minutes for `steps`, `sleep`, `exercise`, `weight`, and `heart-rate` data from the last 7 days on first run.

## Full Configuration

```yaml
sources:
  my_health:
    type: google_health
    token_file: "data/google_token.json"
    poll_interval: "30m"
    lookback_days: 14
    data_types:
      - steps
      - sleep
      - exercise
      - weight
      - heart-rate
      - body-fat
      - distance
      - floors
      - daily-resting-heart-rate
      - oxygen-saturation
```

## Configuration Options

| Option           | Type       | Default                                                  | Description                                                                                                  |
|:-----------------|:-----------|:---------------------------------------------------------|:-------------------------------------------------------------------------------------------------------------|
| `token_file`     | `string`   | *(required)*                                             | Path to the Google OAuth2 token JSON file.                                                                   |
| `poll_interval`  | `interval` | `"10m"`                                                  | How often to poll the API. Supports human-readable strings like `"5m"`, `"1h"`.                              |
| `data_types`     | `string[]` | `["steps", "sleep", "exercise", "weight", "heart-rate"]` | Which health data types to fetch. Uses the kebab-case names from the API (see Supported Data Types below).   |
| `lookback_days`  | `integer`  | `7`                                                      | How many days back to look on the very first poll (before a cursor is saved).                                |
| `coalesce`       | `list`     | `[]`                                                     | Coalescing rules. See [Event Coalescing](coalescing.md).                                                     |

## Supported Data Types

The `data_types` list accepts the kebab-case identifiers from the Google Health API. Common ones include:

| Data Type                    | Category         | Description                              |
|:-----------------------------|:-----------------|:-----------------------------------------|
| `steps`                      | Interval         | Daily step counts.                       |
| `distance`                   | Interval         | Distance traveled.                       |
| `floors`                     | Interval         | Floors climbed.                          |
| `active-minutes`             | Interval         | Active minutes.                          |
| `active-zone-minutes`        | Interval         | Minutes in active heart rate zones.      |
| `sleep`                      | Session          | Sleep sessions with stages and summary.  |
| `exercise`                   | Session          | Exercise sessions (runs, walks, etc.).   |
| `weight`                     | Sample           | Body weight measurements.                |
| `body-fat`                   | Sample           | Body fat percentage.                     |
| `heart-rate`                 | Sample           | Heart rate observations.                 |
| `oxygen-saturation`          | Sample           | SpO2 readings.                           |
| `daily-resting-heart-rate`   | Daily summary    | Resting heart rate per day.              |
| `daily-heart-rate-variability` | Daily summary  | HRV per day.                             |
| `daily-respiratory-rate`     | Daily summary    | Respiratory rate per day.                |

For the full list, see the [API reference](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints).

## Event Definitions

Each data point becomes one event. The `event_type` is derived from the data type name.

| Type                                  | Entity ID       | Description                                    |
|:--------------------------------------|:----------------|:-----------------------------------------------|
| `google.health.steps`                 | Data point ID   | A steps interval data point.                   |
| `google.health.sleep`                 | Data point ID   | A sleep session with stages and summary.       |
| `google.health.exercise`              | Data point ID   | An exercise session.                           |
| `google.health.weight`                | Data point ID   | A weight measurement.                          |
| `google.health.heart_rate`            | Data point ID   | A heart rate observation.                      |
| `google.health.body_fat`              | Data point ID   | A body fat measurement.                        |
| `google.health.distance`              | Data point ID   | A distance interval data point.                |
| `google.health.daily_resting_heart_rate` | Data point ID | Daily resting heart rate summary.             |

The pattern is `google.health.<data_type>` where hyphens in the data type name are replaced with underscores.

## How It Works

1. **Polling**: On each tick, the source fetches data points from the Google Health API for each configured data type.
2. **Time filtering**: The API request includes a time filter based on the last saved cursor (or `lookback_days` on first run). This avoids re-fetching old data.
3. **Pagination**: If the API returns a `nextPageToken`, the source automatically fetches subsequent pages.
4. **Deduplication**: Each data point gets a stable `event_id` derived from its API resource name. The pipeline's built-in deduplication prevents the same data point from being stored twice.
5. **Cursor**: After each successful poll, the current UTC timestamp is saved as the cursor for the next poll.

## Example Event Payload

A steps event might look like:

```json
{
  "event_id": "ghealth_steps_dp1",
  "event_type": "google.health.steps",
  "entity_id": "dp1",
  "occurred_at": "2026-04-14T00:00:00+00:00",
  "data": {
    "steps": {
      "count": "5432",
      "interval": {
        "startTime": "2026-04-14T00:00:00Z",
        "endTime": "2026-04-14T23:59:59Z"
      }
    }
  }
}
```

## Error Handling

- If one data type fails (e.g., 403 Forbidden because the scope wasn't granted), the source logs the error and continues fetching the remaining data types.
- Transient errors are retried on the next poll cycle.
