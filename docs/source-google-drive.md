# Google Drive Source

The Google Drive source watches your Drive for file changes and emits structured events when files are created, moved, trashed, shared, removed, or updated. It uses Google's changes API with a stored cursor for efficient incremental sync.

A key feature is **debounced updates**: when a document is actively edited, the pipeline can wait for a quiet period before emitting a single `file_updated` event instead of flooding your sinks with every intermediate save. This is achieved via the centralized [Coalescing](coalescing.md) system.

## Getting Started

### 1. Authorize access

Generate a Google OAuth token with the `drive` scope using the [Google Auth CLI](google-auth-cli.md):

```bash
inboxclaw google auth \
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
    coalesce:
      - match: ["google.drive.file_updated", "google.drive.file_moved"]
        strategy: "debounce"
        window: "60s"
```

### 3. Initial baseline

On the first run, the source establishes a Drive changes cursor with `getStartPageToken()` and does not crawl or cache your existing Drive files. Existing files are initialized if they later appear in the changes feed, and they are not reported as newly created unless Drive metadata shows they were created or shared after the trusted baseline.

## Core Concepts

### Change Classification

When a file change is detected, the source compares the new metadata against its cached snapshot and classifies the change:

- **Immediate events**: `file_created`, `file_moved`, `file_trashed`, `file_untrashed`, `file_shared_with_you`, `file_removed` — emitted right away.
- **`file_created`**: Only emitted for owned files whose Drive `createdTime` is newer than the trusted baseline. First-seen cache misses before the baseline are cached silently.
- **`file_shared_with_you`**: Emitted for non-owned files newly visible after the trusted baseline. Drive normally provides `sharedWithMeTime` and `sharingUser`; for files visible through group/user permissions where Drive does not provide a share timestamp, `createdTime` is used as the trusted signal and `sharingUser` may be absent.
- **`file_updated`**: Emitted only for meaningful metadata or content changes. Parent changes are reported as `file_moved`, trash state changes as trash/untrash events, and provider-only or empty updates are suppressed. Structural-only changes do not also emit a low-value update containing only `modificationDate`. Because files are often edited in bursts, it is highly recommended to use [Coalescing](coalescing.md) (Debounce) for this event type to avoid noise.

### Recovery, filters, and My Drive scope

If the Drive changes cursor expires, the source resets to a fresh cursor with `getStartPageToken()` and establishes a new trusted baseline without crawling Drive. Events that happened during the gap are not inferred; future changes continue normally. Cached snapshots are kept so later updates can still compare against the last known state.

Filters apply to every event type, including removals. For removed files, `file_id` filters always work; `name` and `parent_id` filters use the last cached snapshot when available.

When `restrict_to_my_drive: true`, incremental polling uses Drive's `restrictToMyDrive` changes option. This is not the same as filtering to files owned by you.

If content fetching for a diffable file hits an API error such as auth failure or rate limiting, the source does not advance the cursor for that page. This allows the change to be retried instead of emitting a misleading update with only timestamp metadata.

### Coalescing (Debounce)

By default, the Google Drive source emits events as they are detected. To avoid a flood of `file_updated` events during an active editing session, you should configure a `coalesce` rule in your `config.yaml`:

```yaml
sources:
  my_drive:
    type: google_drive
    coalesce:
      - match: ["google.drive.file_updated", "google.drive.file_moved"]
        strategy: "debounce"
        window: "60s"
```

This configuration ensures that if you save a file multiple times within 60 seconds, you only receive one final event after 60 seconds of silence. Coalescing intentionally keeps the latest event data; disable coalescing if every raw update event is needed.

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
    restrict_to_my_drive: false
    include_corpus_removals: false
    eligible_mime_types_for_content_diff:
      - "application/vnd.google-apps.document"
      - "text/plain"
      - "text/markdown"
      - "text/html"
    max_diffable_file_bytes: 10485760
    max_changed_sections: 5
    max_section_chars: 300
    filters:
      - ignore_temp:
          in: name
          regex: "^~.*"
      - specific_file:
          in: file_id
          contains: "1AbCd..."
      - ignored_folder:
          in: parent_id
          contains: "0AFolder..."
    coalesce:
      - match: ["google.drive.file_updated", "google.drive.file_moved"]
        strategy: "debounce"
        window: "60s"
```

### Configuration Reference

| Parameter                            | Type     | Default                          | Description                                                                                     |
|:-------------------------------------|:---------|:---------------------------------|:------------------------------------------------------------------------------------------------|
| `token_file`                         | `string` | Required                         | Path to the Google OAuth2 token file.                                                           |
| `poll_interval`                      | `string` | `"10m"`                          | How often to check for changes. Supports human-readable intervals (e.g. `"30s"`, `"5m"`).       |
| `restrict_to_my_drive`               | `bool`   | `false`                          | `true` limits incremental changes to My Drive. It is not an "owned by me" filter. |
| `include_corpus_removals`            | `bool`   | `false`                          | Request corpus-removal details when available.                                                  |
| `eligible_mime_types_for_content_diff`| `list`  | Google Docs, `text/plain`, `text/markdown`, `text/html` | MIME types eligible for paragraph-level text diffing.                         |
| `max_diffable_file_bytes`            | `int`    | `10485760` (10 MB)               | Size limit for content fetching and diffing.                                                    |
| `max_changed_sections`               | `int`    | `5`                              | Maximum number of changed text sections included in a diff payload.                             |
| `max_section_chars`                  | `int`    | `300`                            | Maximum characters per changed text section before adding a `(truncated)` marker.                |
| `filters`                           | `list`   | `[]`                             | List of filters to exclude files by `file_id`, `name`, or `parent_id` using `contains` or `regex`. |
| `coalesce`                           | `list`   | `[]`                             | List of [Coalescing Rules](coalescing.md) (e.g., for `google.drive.file_updated`).              |

## Event Definitions

| Type                                  | Entity ID     | Description                                                    |
|:--------------------------------------|:--------------|:---------------------------------------------------------------|
| `google.drive.file_created`           | Drive file ID | Owned file created after the trusted baseline.                  |
| `google.drive.file_moved`             | Drive file ID | Parent folder changed.                                         |
| `google.drive.file_trashed`           | Drive file ID | File was moved to trash.                                       |
| `google.drive.file_untrashed`         | Drive file ID | File was restored from trash.                                  |
| `google.drive.file_shared_with_you`   | Drive file ID | A non-owned file became visible to you after the trusted baseline. |
| `google.drive.file_removed`           | Drive file ID | File was removed in the changes feed or became inaccessible when processing a change. |
| `google.drive.file_updated`           | Drive file ID | Meaningful content or metadata update.                         |

> `google.drive.file_deleted` and `google.drive.file_permission_changed` are intentionally not emitted in the current version. Deletions/removals are represented as `google.drive.file_removed`.

### Event Examples

#### `google.drive.file_created`

```json
{
  "id": 1,
  "event_id": "drive-1AbCd-google.drive.file_created-2026-03-15T00:40:10Z",
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
    "parentIds": ["0AExampleFolder"],
    "modificationDate": "2026-03-15T00:47:56Z",
    "changes": {
      "modificationDate": {
        "before": "2026-03-15T00:46:12Z",
        "after": "2026-03-15T00:47:56Z"
      },
      "description": {
        "before": "Draft roadmap",
        "after": "Approved roadmap"
      }
    },
    "contentDiff": {
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

For text files with eligible MIME types, `file_updated` includes a `contentDiff` object with `changes` (array of changed text sections), `totalChangedSections`, `addedCharCount`, and `removedCharCount`. Metadata changes are reported in the top-level `changes` object as `{ before, after }` pairs.

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
| `file_created`        | `parentIds`, `createdTime`, `modificationDate`, `description`, `indexableText`, `lastModifyingUser`, `webViewLink`, `size` |
| `file_moved`          | `parentIds: { before, after }`, `owners`, `lastModifyingUser`, `webViewLink`, `size` |
| `file_trashed`        | `trashedBefore`, `trashedAfter`, `owners`, `lastModifyingUser`, `webViewLink`, `size` |
| `file_untrashed`      | `trashedBefore`, `trashedAfter`, `owners`, `lastModifyingUser`, `webViewLink`, `size` |
| `file_shared_with_you`| `sharedWithMeTime`, optional `sharingUser`, `owners`, `modificationDate`, `lastModifyingUser`, `webViewLink`, `size` |
| `file_removed`        | `lastKnownName`, `lastKnownMimeType`, `lastKnownParentIds`; these may be empty for untracked removals |
| `file_updated`        | `modificationDate`, `description`, `lastModifyingUser`, `webViewLink`, `size`, `changes: { ... }`, optional `contentDiff: { ... }` |
