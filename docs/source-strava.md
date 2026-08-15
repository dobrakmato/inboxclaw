# Strava Source

The Strava source polls the authenticated athlete's recent activities and turns them into Inboxclaw events. It emits an event when an activity is first seen and another when a material field in a recently seen activity changes.

## Setup

### 1. Create a Strava API application

Create an application from [Strava's API settings](https://www.strava.com/settings/api). Copy its **Client ID**, **Client Secret**, and **Refresh Token**.

For a personal Inboxclaw installation, the credentials shown on that page are enough. For a multi-athlete application, complete Strava's OAuth authorization-code flow for each athlete and use that athlete's refresh token.

The activity-list endpoint requires `activity:read`. A token with only that scope does not return activities whose visibility is **Only You**; request `activity:read_all` if the source must ingest private activities too.

### 2. Store credentials

Add the credentials to `.env`:

```dotenv
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=replace-me
STRAVA_REFRESH_TOKEN=replace-me
```

Do not commit `.env`. Inboxclaw exchanges the refresh token for Strava's short-lived access token automatically. Because Strava may rotate the refresh token during that exchange, Inboxclaw stores the latest access and refresh tokens in the source KV table in the configured database. Protect the database as credential-bearing data.

If you replace `STRAVA_REFRESH_TOKEN`, Inboxclaw detects the new configured token and stops using the previously persisted token chain.

### 3. Configure the source

```yaml
sources:
  strava:
    poll_interval: "15m"
    lookback_days: 7
```

When the source key is not `strava`, specify the type explicitly:

```yaml
sources:
  my_activities:
    type: strava
    client_id: "${STRAVA_CLIENT_ID}"
    client_secret: "${STRAVA_CLIENT_SECRET}"
    refresh_token: "${STRAVA_REFRESH_TOKEN}"
```

## Behavior

Every poll requests activities whose activity start time falls within the rolling `lookback_days` window. Results are paginated and published oldest first.

- The first successful poll emits `strava.activity_created` for every activity in the window.
- Later polls emit `strava.activity_created` for newly seen activities.
- If a previously seen activity's summary changes, the source emits `strava.activity_updated` with a top-level `changed_fields` diff.
- Social counters are ignored by default, so new kudos or comments do not produce update events.
- The source does not emit deletion events. Polling cannot reliably distinguish deletion from visibility changes or an activity leaving the rolling window.

Each activity version has a deterministic event ID. Inboxclaw's normal `(source_id, event_id)` deduplication therefore makes a retry safe if the process stops between writing an event and updating the source snapshot.

The rolling window also means that a newly uploaded activity whose start date is older than `lookback_days` will not be discovered. Increase `lookback_days` if delayed or historical uploads are common.

## Configuration reference

| Parameter | Type | Default | Description |
|:--|:--|:--|:--|
| `client_id` | string | `STRAVA_CLIENT_ID` | Strava application client ID. |
| `client_secret` | string | `STRAVA_CLIENT_SECRET` | Strava application client secret. |
| `refresh_token` | string | `STRAVA_REFRESH_TOKEN` | Refresh token for the athlete. |
| `poll_interval` | interval | `"15m"` | Time between successful or failed poll attempts. |
| `lookback_days` | integer | `7` | Rolling activity-start window fetched on every poll. Must be at least 1. |
| `per_page` | integer | `100` | Activities requested per API page. Allowed range: 1-200. |
| `ignored_fields` | list of strings | Social count fields | Top-level activity fields excluded from update detection. |
| `coalesce` | list | `[]` | Standard source coalescing rules. |

The default ignored fields are `achievement_count`, `athlete_count`, `comment_count`, `kudos_count`, `photo_count`, and `total_photo_count`. Override the list if changes to any of those fields should produce `strava.activity_updated`.

## Events

### `strava.activity_created`

```json
{
  "event_id": "strava-123456789-created",
  "event_type": "strava.activity_created",
  "entity_id": "123456789",
  "occurred_at": "2026-08-14T06:30:00Z",
  "data": {
    "activity_id": "123456789",
    "activity": {
      "id": 123456789,
      "name": "Morning Run",
      "sport_type": "Run",
      "distance": 5000.0,
      "moving_time": 1500,
      "start_date": "2026-08-14T06:30:00Z"
    }
  }
}
```

### `strava.activity_updated`

```json
{
  "event_id": "strava-123456789-updated-9c7457e63c7a2a5d",
  "event_type": "strava.activity_updated",
  "entity_id": "123456789",
  "occurred_at": "2026-08-14T06:30:00Z",
  "data": {
    "activity_id": "123456789",
    "changed_fields": {
      "name": {
        "before": "Morning Run",
        "after": "Morning 5K"
      }
    },
    "activity": { "...": "current Strava summary activity" }
  }
}
```

`occurred_at` is the activity's UTC `start_date`, not the time Inboxclaw discovered the change.

## Rate limits and polling tradeoff

Strava applies both short-term and daily application limits. The default `15m` poll interval usually costs about 96 list requests per day plus pagination and token refreshes, but a large lookback can add more pages. HTTP 429 responses mark the source unhealthy and the next configured poll retries.

Strava recommends webhooks instead of polling for larger applications and for authoritative update, deletion, privacy-change, and deauthorization notifications. This source intentionally implements the repository's polling model; it does not expose a public webhook endpoint.

## Troubleshooting

- **Authentication failure:** verify all three credential values. If the app was deauthorized, create a new refresh token and restart Inboxclaw.
- **Private activities are missing:** authorize the athlete with `activity:read_all`, not only `activity:read`.
- **Older activities are missing:** increase `lookback_days`; the filter is based on activity start time.
- **Too many updates:** add noisy top-level SummaryActivity fields to `ignored_fields`.
- **Rate limited:** increase `poll_interval` and reduce unnecessary backfill pagination.

See the official [Strava authentication documentation](https://developers.strava.com/docs/authentication/), [API reference](https://developers.strava.com/docs/reference/), and [rate-limit documentation](https://developers.strava.com/docs/rate-limits/).
