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

On the first run, the source performs a baseline sync — it fetches current events inside the configured future window to build its internal cache, but does **not** emit them as new events. This prevents flooding your pipeline with historical data. After the baseline, actual changes and events that newly enter the future window can produce events.

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

### Sync Model and Rolling Lookahead

The source uses Google Calendar incremental sync (`syncToken`) for ordinary changes. Google does not allow `timeMin` or `timeMax` on `syncToken` requests, so the source also keeps a per-calendar rolling lookahead cursor. After a successful incremental poll, it scans only the newly added slice between the previous horizon and the current `now + max_into_future` horizon.

That rolling scan is discovery-only:
- Events that were created far beyond `max_into_future` are ignored at first, then emitted as `created` if they later enter the configured window.
- Missing events from the rolling scan are **not** treated as deleted.
- If the rolling cursor is missing on an existing installation, it is initialized silently instead of scanning the whole window and creating a fake `created` cascade.
- The rolling cursor is advanced only after event writes and cache updates succeed.
- Recurring events are always expanded into individual instances so downstream consumers receive useful per-occurrence changes.

### Time Filtering

Events are filtered by age and future distance:
- Non-recurring events and expanded recurring instances whose scheduled end/start time has already passed are not emitted as updates or deletions.
- `max_event_age_days` (default: `2.0`) — cached snapshots older than this many days are dropped. Future events are still tracked even if they were created or updated long ago.
- `max_into_future` (default: `"365d"`) — events starting after this rolling cutoff are ignored until they enter the window.
- Changing `max_into_future` rebuilds the baseline without emitting a created/deleted cascade for the configuration change itself.

### Deleted Events

Cancelled or deleted entries are tracked for current and future events. When an event is cancelled, a meeting invitation is declined, or Google emits a deletion tombstone for a recurring instance within the configured window, the source emits a `deleted` event so downstream state stays aligned with the useful calendar stream. Events that already ended in the past are removed from the local cache without emitting deletion noise.

## Configuration

### Filtering

You can filter out events based on their properties using regular expressions or simple substring matching. This is useful for ignoring internal meetings, focus time, or events from specific people.

#### Supported Fields

| Field         | Description                                                                 |
| :------------ | :-------------------------------------------------------------------------- |
| `summary`     | The title of the event.                                                     |
| `description` | The event description/notes.                                                |
| `location`    | The physical or virtual location.                                           |
| `organizer`   | The email and display name of the person or calendar that organized the event. |
| `attendees`   | A space-separated list of all attendee email addresses.                     |

#### Example: Ignore All-Hands and Focus Time

```yaml
sources:
  my_calendar:
    type: google_calendar
    filters:
      - ignore_all_hands:
          in: summary
          contains: "All-Hands"
      - ignore_focus_time:
          in: summary
          regex: ".*Focus Time.*"
      - internal_only:
          in: attendees
          regex: ".*@external-vendor\\.com"
```

### Minimal Configuration

```yaml
sources:
  my_calendar:
    type: google_calendar
    token_file: "data/google_token.json"
```

Defaults: `calendar_ids: ["primary"]`, `poll_interval: "10m"`, `max_event_age_days: 2.0`, `max_into_future: "365d"`, `attendee_detail_limit: 3`.

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
    attendee_detail_limit: 3
    calendar_overrides:
      "team@group.calendar.google.com":
        max_into_future: "365d"
```

### Configuration Reference

| Parameter                   | Type     | Default       | Description                                                                                                                                                 |
|:----------------------------|:---------|:--------------|:------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `token_file`                | `string` | Required      | Path to the Google OAuth2 token file.                                                                                                                       |
| `calendar_ids`              | `list`   | `["primary"]` | Google Calendar IDs to monitor.                                                                                                                             |
| `poll_interval`             | `string` | `"10m"`       | How often to check for changes. Supports human-readable intervals.                                                                                          |
| `max_event_age_days`        | `float`  | `2.0`         | Drop cached snapshots older than this many days. Set to `null` to disable cache-age cleanup.                                                                |
| `max_into_future`           | `string` | `"365d"`      | Ignore events starting after this rolling time horizon until they enter the window.                                                                         |
| `attendee_detail_limit`     | `int`    | `3`           | Include individual attendee objects only when the event has at most this many attendees. Larger events emit attendee counts by RSVP state instead.          |
| `calendar_overrides`        | `dict`   | `{}`          | Per-calendar overrides for `max_into_future`. Keyed by calendar ID.                                                                                        |
| `filters`                   | `list`   | `[]`          | List of filters to ignore specific events.                                                                                                                  |

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

Contains a list of attendee status changes. If the event exceeds `attendee_detail_limit`, the RSVP payload is summarized instead of naming individual attendees:

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
When an event has more attendees than `attendee_detail_limit`, the `attendees` array is replaced with `{ "total": N, "by_state": { ... } }` so individual attendee details are not emitted.
For large-event RSVP changes, `rsvp_changes` becomes `{ "changed": N, "before": { ... }, "after": { ... } }`.
