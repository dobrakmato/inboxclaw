# Google Drive Source

The Google Drive source polls for file changes in your Google Drive and ingests them as events into the pipeline.

## Pros & Cons

### Pros
- Efficient incremental updates using Google's `changes` API.
- Captures file modifications, deletions, and additions.
- Includes file metadata like name, MIME type, and owners.

### Cons
- Polling-based (default 5m).
- Requires a valid OAuth token with `drive.readonly` scope.

## Core Concepts

- **Cursors (startPageToken)**: We store a "start page token" from Google Drive. On each poll, we ask for all changes that happened *since* that token. This ensures we don't miss anything and don't re-process old events.
- **Event Deduplication**: Each change is assigned a unique event ID based on the file ID and the time of the change.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_drive:
    type: "google_drive"
    token_file: "data/google_token.json"
```

### Full Configuration

```yaml
sources:
  my_drive:
    type: "google_drive"
    token_file: "data/google_token.json"
    poll_interval: "5m"  # Human-readable interval
```

## Event Definitions

| Parameter | Value | Description |
| :--- | :--- | :--- |
| **Type** | `drive.file_change` | Triggered when a file in the Drive is modified, added, or deleted. |
| **Entity ID** | Google File ID | Uniquely identifies the file. |

### Data Payload

The `data` field contains:

- `fileId`: The ID of the file.
- `removed`: Boolean, true if the file was removed.
- `time`: RFC 3339 timestamp of the change.
- `file`: Metadata object (`id`, `name`, `mimeType`, `modifiedTime`, `owners`) if not removed.
