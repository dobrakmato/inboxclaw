# Google Calendar Source

The Google Calendar source tracks event changes (creations, updates, deletions) in a specified Google Calendar.

## Pros & Cons

### Pros
- Incremental syncing using Google's `syncToken` API.
- Captures deletions through the `status: cancelled` flag in event items.
- Works with the primary calendar or any other shared calendar ID.

### Cons
- Polling-based (default 10m).
- Initial sync might only fetch upcoming events (from "now" onwards) if no token is present.

## Core Concepts

- **Sync Tokens**: We store a `syncToken` returned by the Google Calendar API. On subsequent polls, we use this token to request *only* the changes (increments) that occurred since the last request.
- **Event ID**: The internal event ID is a combination of the Google Event ID and its `updated` timestamp, ensuring unique events for each revision.

## Configuration

### Finding your Calendar ID

If you want to monitor a calendar other than your `primary` one, you can list all available calendars (names and IDs) using the CLI:

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
    calendar_id: "primary"  # Or email address of shared calendar
    poll_interval: "10m"
```

## Event Definitions

| Parameter | Value | Description |
| :--- | :--- | :--- |
| **Type** | `calendar.event_change` | Triggered when a calendar event is created, updated, or deleted. |
| **Entity ID** | Google Event ID | Uniquely identifies the calendar event. |

### Data Payload

The `data` field contains the full [Google Calendar Event resource](https://developers.google.com/calendar/api/v3/reference/events#resource).
