# Google Calendar Source

The Google Calendar source provides a robust way to integrate calendar activity into your business processes. Whether you're building a CRM that needs to log meetings, a resource management tool that tracks employee availability, or an automated reporting system for client consultations, this source ensures your application stays in sync with real-world schedules.

By monitoring one or more Google Calendars, this source allows you to react instantly when meetings are scheduled, moved, or cancelled. It's particularly useful for:
- **CRM Integration**: Automatically log when a sales call is created or its duration is updated.
- **Resource Tracking**: Monitor room bookings or equipment usage scheduled via shared calendars.
- **Response Management**: Track attendee RSVPs to ensure meeting attendance is properly accounted for.
- **Workflow Automation**: Trigger follow-up tasks immediately after a meeting ends.

## Implementation Details

The Google Calendar source is designed for both efficiency and depth, using a combination of Google's incremental sync APIs and a local state cache to provide high-quality event data.

- **Incremental Syncing**: To minimize API usage and latency, the source uses Google's `syncToken` mechanism. After an initial baseline sync, it only requests changes that have occurred since the last poll.
- **Intelligent Change Detection**: Unlike basic sync tools that simply tell you "something changed," this source compares new event data against its local KV cache. This allows it to distinguish between a general update (like a title or time change) and an RSVP-only change where an attendee updated their status.
- **Event Versioning**: Each event emitted is uniquely versioned using a combination of the Google Event ID and its `etag` or `updated` timestamp. This ensures that every distinct revision is captured and can be processed independently.
- **Baseline Behavior**: Upon first startup (or if a `syncToken` expires), the source performs a baseline sync. It fetches all current events from "now" onwards but does not emit them as new events. This prevents flooding your system with hundreds of historical events when you first connect a calendar.

## Core Concepts

- **Coalescing**: The source fetches raw event resources from Google and classifies them into specific, actionable event types (`created`, `updated`, `deleted`, `rsvp_changed`).
- **Time-based Filtering**: To keep your pipeline focused on relevant data, the source automatically ignores events that are too old (based on `max_event_age_days`) or too far in the future (`max_into_future`).

## Configuration

### Finding your Calendar IDs

If you want to monitor calendars other than your `primary` one, you can list all available calendars (names and IDs) using the CLI:

```bash
python main.py google list-calendars --token-file data/google_token.json
```

### Minimal Configuration

```yaml
sources:
  my_calendar:
    type: "google_calendar"
    token_file: "data/google_token.json"
```

### Full Configuration

```yaml
sources:
  my_calendar:
    type: "google_calendar"
    token_file: "data/google_token.json"
    calendar_ids: 
      - "primary"
      - "another-calendar-id@group.calendar.google.com"
    poll_interval: "5m"
    max_event_age_days: 7.0
    max_into_future: "30d"
    calendar_overrides:
      "another-calendar-id@group.calendar.google.com":
        max_into_future: "365d"
        single_events: false
    show_deleted: true
    single_events: true
```

### Configuration Parameters

| Parameter            | Type      | Default     | Description                                                                                                                                                                                                                                                                  |
|:---------------------|:----------|:------------|:-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `token_file`         | `string`  | Required    | Path to the Google OAuth2 token file (created via CLI).                                                                                                                                                                                                                      |
| `calendar_ids`       | `list`    | `[primary]` | List of Google Calendar IDs to monitor.                                                                                                                                                                                                                                      |
| `poll_interval`      | `string`  | `10m`       | How often to check for changes (e.g., "5s", "1m", "1h").                                                                                                                                                                                                                     |
| `max_event_age_days` | `float`   | `1.0`       | Maximum age of events to process and keep in cache. Events with `occurred_at` older than this are dropped. A background task cleans up the KV cache daily based on this value. Set to `null` to disable filtering.                                                               |
| `max_into_future`    | `string`  | `365d`      | Maximum time into the future to sync events (e.g., "30d", "1y"). Events starting after this cutoff are ignored.                                                                                                                                                               |
| `calendar_overrides` | `dict`    | `{}`        | Per-calendar overrides for `max_into_future`, `show_deleted`, and `single_events`. Keyed by calendar ID.                                                                                                                                                                     |
| `show_deleted`       | `boolean` | `true`      | Whether to include deleted events in the sync. See below for more details.                                                                                                                                                                                                   |
| `single_events`      | `boolean` | `true`      | Whether to expand recurring events into instances. See below for more details.                                                                                                                                                                                               |

### Choosing Configuration Values

#### `single_events`

This parameter determines how the source handles recurring meetings (e.g., a "Weekly Sync").

- **Set to `true` (Recommended for most uses)**: Every single occurrence of a meeting is treated as its own event.
    - *Example*: If you have a "Weekly Sync" every Monday, moving just *one* specific Monday's meeting to Tuesday will emit an `updated` event for that specific instance.
    - *Use Case*: Use this when your application needs to track attendance or notes for specific meeting instances.
- **Set to `false`**: Only the "master" recurring event is tracked.
    - *Example*: You only get an event when the entire series is created, or when the overall schedule (e.g., "change from weekly to bi-weekly") is updated.
    - *Use Case*: Use this if you only care about the existence of a series and don't need to track individual occurrences.

#### `show_deleted`

This controls whether the source "remembers" and reports on meetings that have been removed.

- **Set to `true`**: When an event is deleted or a meeting invitation is declined (for your primary calendar), a `google.calendar.event.deleted` event is emitted.
    - *Example*: A client cancels a consultation. Your system receives a `deleted` event and can automatically free up that slot in your own internal database or send a cancellation confirmation.
    - *Use Case*: Critical for any system that needs to maintain a perfectly mirrored state of a Google Calendar.
- **Set to `false`**: Deleted events are simply ignored and never emitted.
    - *Use Case*: Use this if your system only cares about what *is* happening, not what was *cancelled*.

## Event Definitions

| Type                                 | Entity ID       | Description                                                               |
|:-------------------------------------|:----------------|:--------------------------------------------------------------------------|
| `google.calendar.event.created`      | Google Event ID | Triggered when a new calendar event is discovered.                        |
| `google.calendar.event.updated`      | Google Event ID | Triggered when an existing event's properties (title, time, etc.) change. |
| `google.calendar.event.deleted`      | Google Event ID | Triggered when an event is cancelled or deleted.                          |
| `google.calendar.event.rsvp_changed` | Google Event ID | Triggered when one or more attendees change their response status.        |

### Data Payload Examples

All events include the standard `data` field. Depending on the event type, this field contains different structures:

#### `google.calendar.event.created`
Contains the event ID, minimal context, and the full Google Event resource.
```json
{
  "event_id": "7abc123...",
  "summary": "Project Kickoff",
  "start": { "dateTime": "2024-10-10T10:00:00Z" },
  "event": {
    "id": "7abc123...",
    "summary": "Project Kickoff",
    "start": { "dateTime": "2024-10-10T10:00:00Z" },
    "end": { "dateTime": "2024-10-10T11:00:00Z" },
    "status": "confirmed",
    ...
  }
}
```

#### `google.calendar.event.updated`
Contains the event ID, minimal context, and a dictionary of changed fields. Each changed field shows its `before` and `after` state.
```json
{
  "event_id": "7abc123...",
  "summary": "New Title",
  "start": { "dateTime": "2024-10-10T10:30:00Z" },
  "changes": {
    "summary": {
      "before": "Old Title",
      "after": "New Title"
    },
    "start": {
      "before": { "dateTime": "2024-10-10T10:00:00Z" },
      "after": { "dateTime": "2024-10-10T10:30:00Z" }
    }
  }
}
```

#### `google.calendar.event.deleted`
Contains the event ID, minimal context, and the last known states.
```json
{
  "event_id": "7abc123...",
  "summary": "Project Kickoff",
  "start": { "dateTime": "2024-10-10T10:00:00Z" },
  "event": {
    "id": "7abc123...",
    "status": "cancelled",
    ...
  },
  "previous": {
    "id": "7abc123...",
    "summary": "Project Kickoff",
    "status": "confirmed",
    ...
  }
}
```

#### `google.calendar.event.rsvp_changed`
Includes the event ID, minimal context, and a list of specific attendee status changes.
```json
{
  "event_id": "7abc123...",
  "summary": "Project Kickoff",
  "start": { "dateTime": "2024-10-10T10:00:00Z" },
  "rsvp_changes": [
    {
      "attendee": "john.doe@example.com",
      "before": "needsAction",
      "after": "accepted"
    }
  ]
}
```

The `event`, `previous` and values in `changes` follow the [Google Calendar Event resource](https://developers.google.com/calendar/api/v3/reference/events#resource) specification.
