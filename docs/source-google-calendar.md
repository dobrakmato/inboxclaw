# Google Calendar Source

The Google Calendar source monitors one or more Google Calendars and emits events when meetings are created, updated, deleted, or when attendees change their RSVP status. It uses Google's incremental sync (`syncToken`) to fetch only what changed since the last poll.

Use this source to keep your systems in sync with real-world schedules — log meetings in a CRM, track room bookings, trigger follow-ups after meetings end, or react to cancellations.

## Getting Started

### 1. Authorize access

Generate a Google OAuth token with the `calendar` scope using the [Google Auth CLI](google-auth-cli.md):

```bash
inboxclaw google auth \
  --credentials-file data/credentials.json \
  --scopes calendar \
  --token data/google_token.json
```

### 2. Add the source to `config.yaml`

```yaml
sources:
  my_calendar:
    type: google_calendar
    token_file: "data/google_token.json"
```

On the first run, the source performs a baseline sync — it fetches all current events from "now" onwards to build its internal cache, but does **not** emit them as new events. This prevents flooding your pipeline with historical data. After the baseline, only actual changes produce events.

### 3. (Optional) Find your Calendar IDs

By default, the source monitors your `primary` calendar. To monitor additional calendars, list them with the CLI:

```bash
inboxclaw google list-calendars --token-file data/google_token.json
```

Then add the IDs to your config:

```yaml
sources:
  my_calendar:
    type: google_calendar
    token_file: "data/google_token.json"
    calendar_ids:
      - "primary"
      - "team-calendar@group.calendar.google.com"
```

## Core Concepts

### Intelligent Change Detection

The source doesn't just report "something changed." It compares new event data against its local cache to classify changes into specific types: created, updated, deleted, or RSVP changed. For updates, it computes exactly which fields changed (title, time, etc.) and includes before/after values.

### Time Filtering

Events are filtered by age and future distance:
- `max_event_age_days` (default: `1.0`) — events with `occurred_at` older than this are dropped. A background task cleans up the cache daily.
- `max_into_future` (default: `"365d"`) — events starting after this cutoff are ignored.

### Recurring Events (`single_events`)

- **`true`** (default): Each occurrence of a recurring meeting is tracked individually. Moving one Monday's meeting to Tuesday emits an `updated` event for that specific instance.
- **`false`**: Only the master recurring event is tracked. You get events when the entire series is created or its schedule changes, but not for individual occurrences.

### Event Collapsing (`collapse_recurring_events`)

- **`true`** (default): When multiple instances of the same recurring series change in a single sync batch (e.g., when you edit a series "from this event onwards"), the source emits only **one** event instead of one for every single occurrence. This prevents pipeline flooding.
- **`false`**: Every changed occurrence is reported individually.

### Deleted Events (`show_deleted`)

- **`true`** (default): When an event is cancelled or a meeting invitation is declined, a `deleted` event is emitted. Use this when your system needs to mirror the calendar state exactly.
- **`false`**: Deleted events are silently ignored.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_calendar:
    type: google_calendar
    token_file: "data/google_token.json"
```

Defaults: `calendar_ids: ["primary"]`, `poll_interval: "10m"`, `max_event_age_days: 1.0`, `max_into_future: "365d"`, `show_deleted: true`, `single_events: true`.

### Full Configuration

```yaml
sources:
  my_calendar:
    type: google_calendar
    token_file: "data/google_token.json"
    calendar_ids:
      - "primary"
      - "team@group.calendar.google.com"
    poll_interval: "5m"
    max_event_age_days: 7.0
    max_into_future: "30d"
    show_deleted: true
    single_events: true
    calendar_overrides:
      "team@group.calendar.google.com":
        max_into_future: "365d"
        single_events: false
```

### Configuration Reference

| Parameter                   | Type     | Default       | Description                                                                                                                                                 |
|:----------------------------|:---------|:--------------|:------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `token_file`                | `string` | Required      | Path to the Google OAuth2 token file.                                                                                                                       |
| `calendar_ids`              | `list`   | `["primary"]` | Google Calendar IDs to monitor.                                                                                                                             |
| `poll_interval`             | `string` | `"10m"`       | How often to check for changes. Supports human-readable intervals.                                                                                          |
| `max_event_age_days`        | `float`  | `1.0`         | Drop events older than this many days. Set to `null` to disable.                                                                                            |
| `max_into_future`           | `string` | `"365d"`      | Ignore events starting after this time horizon.                                                                                                             |
| `calendar_overrides`        | `dict`   | `{}`          | Per-calendar overrides for `max_into_future`, `show_deleted`, `single_events`, and `collapse_recurring_events`. Keyed by calendar ID.                       |
| `show_deleted`              | `bool`   | `true`        | Whether to emit events for cancelled/deleted calendar entries.                                                                                              |
| `single_events`             | `bool`   | `true`        | Whether to expand recurring events into individual instances (this is useful for discovering new instances of the same event).                              |
| `collapse_recurring_events` | `bool`   | `true`        | Whether to collapse multiple occurrences of the same recurring event in a single poll batch (this is to avoid getting one update for every event in a row). |

#### Single Events vs. Collapse Recurring Events

##### single_events

This controls whether the source receives individual events for each occurrence of a recurring event.

When this is false, you will get a single event describing every future occurrence. You must expand the event to see individual occurrences yourself. 

Enabling this allows the source to discover each occurrence individually.

##### collapse_recurring_events

This controls whether the source collapses multiple occurrences of the same recurring event into a single event. 

When this is false, the source emits an event for every occurrence of a recurring event. So you will one event update per occurrence.

Setting this to true will collapse multiple occurrences of the same recurring event into a single update.

## Event Definitions

| Type                                 | Entity ID       | Description                                                               |
|:-------------------------------------|:----------------|:--------------------------------------------------------------------------|
| `google.calendar.event.created`      | Google Event ID | A new calendar event was discovered.                                      |
| `google.calendar.event.updated`      | Google Event ID | An existing event's properties (title, time, etc.) changed.               |
| `google.calendar.event.deleted`      | Google Event ID | An event was cancelled or deleted.                                        |
| `google.calendar.event.rsvp_changed` | Google Event ID | One or more attendees changed their response status.                      |

### Event Examples

#### `google.calendar.event.created`

```json
{
  "id": 1,
  "event_id": "7abc123-created-etag1",
  "event_type": "google.calendar.event.created",
  "entity_id": "7abc123",
  "created_at": "2024-10-10T09:00:00+00:00",
  "data": {
    "event_id": "7abc123",
    "summary": "Project Kickoff",
    "start": { "dateTime": "2024-10-10T10:00:00Z" },
    "event": {
      "id": "7abc123",
      "summary": "Project Kickoff",
      "start": { "dateTime": "2024-10-10T10:00:00Z" },
      "end": { "dateTime": "2024-10-10T11:00:00Z" },
      "status": "confirmed"
    }
  },
  "meta": {}
}
```

#### `google.calendar.event.updated`

Contains a `changes` dict with before/after values for each changed field:

```json
{
  "id": 2,
  "event_id": "7abc123-updated-etag2",
  "event_type": "google.calendar.event.updated",
  "entity_id": "7abc123",
  "created_at": "2024-10-10T09:30:00+00:00",
  "data": {
    "event_id": "7abc123",
    "summary": "New Title",
    "start": { "dateTime": "2024-10-10T10:30:00Z" },
    "changes": {
      "summary": { "before": "Old Title", "after": "New Title" },
      "start": {
        "before": { "dateTime": "2024-10-10T10:00:00Z" },
        "after": { "dateTime": "2024-10-10T10:30:00Z" }
      }
    }
  },
  "meta": {}
}
```

#### `google.calendar.event.deleted`

Contains the last known state in `previous` and the current (cancelled) state in `event`:

```json
{
  "id": 3,
  "event_id": "7abc123-deleted-etag3",
  "event_type": "google.calendar.event.deleted",
  "entity_id": "7abc123",
  "created_at": "2024-10-10T10:00:00+00:00",
  "data": {
    "event_id": "7abc123",
    "summary": "Project Kickoff",
    "start": { "dateTime": "2024-10-10T10:00:00Z" },
    "event": { "id": "7abc123", "status": "cancelled" },
    "previous": { "id": "7abc123", "summary": "Project Kickoff", "status": "confirmed" }
  },
  "meta": {}
}
```

#### `google.calendar.event.rsvp_changed`

Contains a list of attendee status changes:

```json
{
  "id": 4,
  "event_id": "7abc123-rsvp-etag4",
  "event_type": "google.calendar.event.rsvp_changed",
  "entity_id": "7abc123",
  "created_at": "2024-10-10T10:15:00+00:00",
  "data": {
    "event_id": "7abc123",
    "summary": "Project Kickoff",
    "start": { "dateTime": "2024-10-10T10:00:00Z" },
    "rsvp_changes": [
      { "attendee": "john@example.com", "before": "needsAction", "after": "accepted" }
    ]
  },
  "meta": {}
}
```

The `event` and `previous` objects follow the [Google Calendar Event resource](https://developers.google.com/calendar/api/v3/reference/events#resource) specification.
