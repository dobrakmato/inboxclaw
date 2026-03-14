# Sources in Ingest Pipeline

Sources are the entry points of data into the system. They are responsible for fetching data from external systems (like Google APIs) and publishing it as **Events** to the internal pipeline.

### How they work

1.  **Polling**: Most sources operate on a polling basis, periodically checking for new or updated data.
2.  **Authentication**: Many sources (especially Google-based ones) require authentication. This project uses OAuth2, with tokens managed via a CLI tool.
3.  **Deduplication**: Sources generate unique `event_id`s for each piece of data they fetch. The pipeline uses these IDs to ensure that the same event is not processed multiple times.
4.  **Cursor Management**: To avoid re-fetching all data every time, sources often use a "cursor" (e.g., `syncToken`, `pageToken`, or a timestamp) stored in the database.

### Interaction with the System

Sources interact with the rest of the system primarily through `AppServices`:
-   **Database**: Sources are registered in the `sources` table. Their current cursor state is stored there.
-   **Event Writer**: Sources call `services.writer.write_events()` to save new events to the database.
-   **Source Cursor**: Sources use `services.cursor.get_last_cursor(source_id)` and `services.cursor.set_cursor(source_id, value)` to manage their watermark.

### Cursors (Watermarks)

To ensure efficient data fetching and avoid processing the same data twice, sources use a **Cursor** (also known as a watermark). A cursor represents the last point of progress for a given source.

#### Why use cursors?
- **Efficiency**: Only fetch data that has changed since the last poll.
- **Reliability**: If a source stops, it can resume from exactly where it left off.
- **Reduced Load**: Minimizes API calls to external systems by avoiding full scans.

#### Cursor Types
- **Page/Sync Tokens**: Provided by many APIs (like Google Drive/Calendar) to represent a specific point in the change history.
- **Timestamps**: Used when an API only supports filtering by modification date (like Google Docs search).
- **Sequence Numbers**: Used for systems with monotonically increasing IDs.

### Common Event Parameters

When a source creates a `NewEvent`, it fills the following fields:

| Parameter     | Meaning                                                              | How to fill it                                                                                                                      |
|:--------------|:---------------------------------------------------------------------|:------------------------------------------------------------------------------------------------------------------------------------|
| `event_id`    | A globally unique identifier for this specific event instance.       | Use a unique ID from the external system (e.g., message ID, change ID) or combine an ID with a timestamp/version.                   |
| `event_type`  | A string identifying the type of event.                              | Use a dot-separated string (e.g., `gmail.message_added`, `drive.file_change`).                                                              |
| `entity_id`   | An identifier for the underlying object being reported on.           | Use the ID of the object in the external system (e.g., file ID, email ID). Note that one entity can have multiple events over time. |
| `data`        | A dictionary containing the actual payload of the event.             | Include all relevant metadata or content from the source system.                                                                    |
| `occurred_at` | The timestamp when the event actually happened in the source system. | Use the timestamp provided by the source system (e.g., `internalDate`, `modifiedTime`).                                             |

### Coalescing

Sinks can coalesce events with the same `entity_id` and `event_type` into a single event. Typical examples include:
- Merge multiple document updates events (`event_id`) of the same document (`entity_id`) into a single `document.updated` event.

### Minimal Configuration

A minimal configuration for a source in `config.yaml` usually only requires the type and some basic authentication info:

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: tokens/gmail.json
```

### Full Configuration Example

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: tokens/gmail.json
    poll_interval: "1m"
    max_results: 100
```
