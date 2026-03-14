# Google Calendar Source

The Google Calendar source tracks event changes (creations, updates, deletions, and RSVP changes) in one or more Google Calendars.

## Pros & Cons

### Pros
- Incremental syncing using Google's `syncToken` API for high efficiency.
- Supports multiple calendars within a single source configuration.
- Detailed change detection: distinguishes between general event updates and RSVP-only changes.
- Captures deletions through the `status: cancelled` flag in event items.
- Automatic filtering of old events to keep the pipeline focused on recent activity.

### Cons
- Polling-based (configurable interval).
- Initial sync (baseline) fetch events from "now" onwards and doesn't emit them to avoid flooding.

## Core Concepts

- **Incremental Syncing**: We store a `syncToken` for each calendar. On subsequent polls, we request only the changes that occurred since the last request.
- **Coalescing & DTOs**: The source fetches raw event resources from Google and classifies them into specific event types. It maintains a local snapshot (using KV cache) to perform diffing and detect what exactly changed (e.g., if only an RSVP status changed).
- **Event Versioning**: Each event is versioned using its `etag` or `updated` timestamp, ensuring that every distinct revision is captured.

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
    show_deleted: true
    single_events: true
```

## Event Definitions

| Type                                 | Entity ID       | Description                                                               |
|:-------------------------------------|:----------------|:--------------------------------------------------------------------------|
| `google.calendar.event.created`      | Google Event ID | Triggered when a new calendar event is discovered.                        |
| `google.calendar.event.updated`      | Google Event ID | Triggered when an existing event's properties (title, time, etc.) change. |
| `google.calendar.event.deleted`      | Google Event ID | Triggered when an event is cancelled or deleted.                          |
| `google.calendar.event.rsvp_changed` | Google Event ID | Triggered when one or more attendees change their response status.        |

### Data Payload

The `data` field contains:
- `event`: The current [Google Calendar Event resource](https://developers.google.com/calendar/api/v3/reference/events#resource).
- `previous`: (For updates/deletions) The previous version of the event resource.
- `changes`: (For RSVP changes) A list of specific attendee status changes.
