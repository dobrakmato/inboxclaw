# Google Drive Source

The Google Drive source watches your Drive for file changes and emits structured events when files are created, moved, trashed, shared, removed, or updated. It uses Google's changes API with a stored cursor for efficient incremental sync.

A key feature is **debounced updates**: when a document is actively edited, the source waits for a quiet period before emitting a single `file_updated` event instead of flooding the pipeline with every intermediate save. This makes the output clean and actionable.

## Getting Started

### 1. Authorize access

Generate a Google OAuth token with the `drive` scope using the [Google Auth CLI](google-auth-cli.md):

```bash
python main.py google auth \
  --credentials-file data/credentials.json \
  --scopes drive \
  --token data/google_token.json
```

The `drive` scope grants read-only access to file content, which is needed for text diffs in `file_updated` events. If you only need metadata tracking (names, moves, shares) without text diffs, you can use the `drive_metadata` scope instead and set `eligible_mime_types_for_content_diff: []` in your config.

### 2. Add the source to `config.yaml`

```yaml
sources:
  my_drive:
    type: google_drive
    token_file: "data/google_token.json"
    poll_interval: "30s"
```

### 3. Initial sync (bootstrapping)

On the first run, the source needs to learn about your existing files so it doesn't report them all as "newly created" when they're next modified. The `bootstrap_mode` setting controls this:

- **`baseline_only`** (default): Quick crawl of your Drive to record current file state. No events emitted. Future changes are compared against this baseline.
- **`full_snapshot`**: Like `baseline_only`, but also fetches and caches text content of documents. This allows the very first `file_updated` event to include a text diff. Slower and uses more API quota.
- **`off`**: No initial crawl. All existing files will emit a `file_created` event the first time they are modified after the source starts.

## Core Concepts

### Debounced Updates

Google Docs and similar files can generate many small changes during active editing. Without debounce, one editing session would create a burst of noisy events.

The source handles this with two settings:
- `update_quiet_window` (default: `"60s"`): After the last detected change, wait this long before emitting `file_updated`. If new edits arrive during the window, the timer resets.
- `update_max_session` (default: `"10m"`): Maximum time before forcing an update flush, even if edits are still arriving. This prevents heavily edited files from staying pending forever.

### Change Classification

When a file change is detected, the source compares the new metadata against its cached snapshot and classifies the change:

- **Immediate events**: `file_created`, `file_moved`, `file_trashed`, `file_untrashed`, `file_shared_with_you`, `file_removed` — emitted right away.
- **Debounced event**: `file_updated` — batched and emitted after the quiet window or max session.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_drive:
    type: google_drive
    token_file: "data/google_token.json"
    poll_interval: "30s"
```

### Full Configuration

```yaml
sources:
  my_drive:
    type: google_drive
    token_file: "data/google_token.json"
    poll_interval: "30s"
    bootstrap_mode: "baseline_only"
    restrict_to_my_drive: false
    include_removed: true
    include_corpus_removals: false
    update_quiet_window: "60s"
    update_max_session: "10m"
    eligible_mime_types_for_content_diff:
      - "application/vnd.google-apps.document"
      - "text/plain"
      - "text/markdown"
      - "text/html"
    max_diffable_file_bytes: 10485760
```

### Configuration Reference

| Parameter                            | Type     | Default                          | Description                                                                                     |
|:-------------------------------------|:---------|:---------------------------------|:------------------------------------------------------------------------------------------------|
| `token_file`                         | `string` | Required                         | Path to the Google OAuth2 token file.                                                           |
| `poll_interval`                      | `string` | `"10m"`                          | How often to check for changes. Supports human-readable intervals (e.g. `"30s"`, `"5m"`).       |
| `bootstrap_mode`                     | `string` | `"baseline_only"`                | Initial sync behavior: `baseline_only`, `full_snapshot`, or `off`.                              |
| `restrict_to_my_drive`               | `bool`   | `false`                          | `true` limits scope to My Drive only. `false` allows wider visibility.                          |
| `include_removed`                    | `bool`   | `true`                           | Include removal entries from the Drive changes feed.                                            |
| `include_corpus_removals`            | `bool`   | `false`                          | Request corpus-removal details when available.                                                  |
| `update_quiet_window`                | `string` | `"60s"`                          | Quiet period before emitting a debounced `file_updated` event.                                  |
| `update_max_session`                 | `string` | `"10m"`                          | Maximum wait before forcing an update flush.                                                    |
| `eligible_mime_types_for_content_diff`| `list`  | Google Docs, `text/*` types      | MIME types eligible for paragraph-level text diffing.                                           |
| `max_diffable_file_bytes`            | `int`    | `10485760` (10 MB)               | Size limit for content fetching and diffing.                                                    |

## Event Definitions

| Type                                  | Entity ID     | Description                                                    |
|:--------------------------------------|:--------------|:---------------------------------------------------------------|
| `google.drive.file_created`           | Drive file ID | File first seen in local snapshot cache.                       |
| `google.drive.file_moved`             | Drive file ID | Parent folder changed.                                         |
| `google.drive.file_trashed`           | Drive file ID | File was moved to trash.                                       |
| `google.drive.file_untrashed`         | Drive file ID | File was restored from trash.                                  |
| `google.drive.file_shared_with_you`   | Drive file ID | A file was shared with you (non-owned file).                   |
| `google.drive.file_removed`           | Drive file ID | File was removed from the changes feed (`change.removed=true`).|
| `google.drive.file_updated`           | Drive file ID | Debounced update after version/modifiedTime change.            |

> `google.drive.file_deleted` and `google.drive.file_permission_changed` are intentionally not emitted in the current version.

### Event Examples

#### `google.drive.file_created`

```json
{
  "id": 1,
  "event_id": "drive-1AbCd-file_created-1741999501",
  "event_type": "google.drive.file_created",
  "entity_id": "1AbCd",
  "created_at": "2026-03-15T00:45:01+00:00",
  "data": {
    "fileId": "1AbCd",
    "name": "Q1 plan",
    "mimeType": "application/vnd.google-apps.document",
    "parentIds": ["0AExampleFolder"],
    "owners": [
      {
        "displayName": "Alice",
        "emailAddress": "alice@example.com"
      }
    ],
    "createdTime": "2026-03-15T00:40:10Z",
    "description": "Quarterly roadmap",
    "lastModifyingUser": {
      "displayName": "Alice",
      "emailAddress": "alice@example.com"
    },
    "webViewLink": "https://docs.google.com/document/d/1AbCd/edit?usp=drivesdk",
    "size": "12345"
  },
  "meta": {}
}
```

#### `google.drive.file_updated` (debounced)

```json
{
  "id": 2,
  "event_id": "drive-1AbCd-google.drive.file_updated-27",
  "event_type": "google.drive.file_updated",
  "entity_id": "1AbCd",
  "created_at": "2026-03-15T00:48:10+00:00",
  "data": {
    "fileId": "1AbCd",
    "name": "Q1 plan",
    "mimeType": "application/vnd.google-apps.document",
    "parentIds": {
      "before": ["0AExampleFolder"],
      "after": ["0AExampleFolder"]
    },
    "session": {
      "sessionStartedAt": "2026-03-15T00:46:12Z",
      "lastChangeSeenAt": "2026-03-15T00:47:56Z",
      "rawChangeCount": 4,
      "changes": [
        {
          "before": "Old paragraph content...",
          "after": "New paragraph content..."
        }
      ],
      "totalChangedSections": 1,
      "addedCharCount": 15,
      "removedCharCount": 10
    },
    "lastModifyingUser": {
      "displayName": "Alice",
      "emailAddress": "alice@example.com"
    },
    "webViewLink": "https://docs.google.com/document/d/1AbCd/edit?usp=drivesdk",
    "size": "12345"
  },
  "meta": {}
}
```

For text files with eligible MIME types, `file_updated` includes diff fields under the `session` object: `changes` (array of change objects), `totalChangedSections`, `addedCharCount`, `removedCharCount`.

#### `google.drive.file_moved`

```json
{
  "id": 3,
  "event_id": "drive-1AbCd-google.drive.file_moved-2026-03-15T00:50:01Z",
  "event_type": "google.drive.file_moved",
  "entity_id": "1AbCd",
  "created_at": "2026-03-15T00:50:01+00:00",
  "data": {
    "fileId": "1AbCd",
    "name": "Q1 plan",
    "mimeType": "application/vnd.google-apps.document",
    "parentIds": {
      "before": ["0AExampleFolder"],
      "after": ["0ANewFolder"]
    },
    "owners": [
      {
        "displayName": "Alice",
        "emailAddress": "alice@example.com"
      }
    ],
    "lastModifyingUser": {
      "displayName": "Alice",
      "emailAddress": "alice@example.com"
    },
    "webViewLink": "https://docs.google.com/document/d/1AbCd/edit?usp=drivesdk",
    "size": "12345"
  },
  "meta": {}
}
```

#### `google.drive.file_removed`

```json
{
  "id": 4,
  "event_id": "drive-1AbCd-google.drive.file_removed-2026-03-15T00:51:22Z",
  "event_type": "google.drive.file_removed",
  "entity_id": "1AbCd",
  "created_at": "2026-03-15T00:51:22+00:00",
  "data": {
    "fileId": "1AbCd",
    "lastKnownName": "Q1 plan",
    "lastKnownMimeType": "application/vnd.google-apps.document",
    "lastKnownParentIds": ["0AExampleFolder"]
  },
  "meta": {}
}
```

#### `google.drive.file_shared_with_you`

```json
{
  "id": 5,
  "event_id": "drive-7XyZa-google.drive.file_shared_with_you-2026-03-15T00:52:09Z",
  "event_type": "google.drive.file_shared_with_you",
  "entity_id": "7XyZa",
  "created_at": "2026-03-15T00:52:09+00:00",
  "data": {
    "fileId": "7XyZa",
    "name": "Vendor Contract",
    "mimeType": "application/pdf",
    "owners": [
      {
        "displayName": "Bob",
        "emailAddress": "bob@example.com"
      }
    ],
    "sharedWithMeTime": "2026-03-15T00:52:00Z",
    "sharingUser": {
      "displayName": "Alice",
      "emailAddress": "alice@example.com"
    }
  },
  "meta": {}
}
```

### Data Payload Reference

Common fields across all event types: `fileId`, `name`, `mimeType`, `owners`.

| Event type            | Additional fields                                                                                          |
|:----------------------|:-----------------------------------------------------------------------------------------------------------|
| `file_created`        | `parentIds`, `createdTime`                                                                                 |
| `file_moved`          | `parentIds: { before, after }`                                                                             |
| `file_trashed`        | `trashedBefore`, `trashedAfter`                                                                            |
| `file_untrashed`      | `trashedBefore`, `trashedAfter`                                                                            |
| `file_shared_with_you`| `sharedWithMeTime`, `sharingUser`                                                                          |
| `file_removed`        | `lastKnownName`, `lastKnownMimeType`, `lastKnownParentIds`                                                |
| `file_updated`        | `session: { sessionStartedAt, lastChangeSeenAt, rawChangeCount, totalChangedSections, addedCharCount, removedCharCount, changes: [{before, after}] }` |
