# Google Drive Source

The Google Drive source watches your Drive for file changes and turns those changes into events your pipeline can consume.

In plain language: it checks Google Drive on a schedule, notices what changed (new file, moved file, shared file, etc.), and sends clean structured events downstream.

This source is designed to be stable during real work (for example, when one document is edited many times in a short period).

## What this source does

- Polls Google Drive `changes` regularly.
- Stores file snapshots in KV state so it can understand transitions (before vs after).
- Emits immediate metadata events (create/move/trash/share/removed).
- Emits `google.drive.file_updated` with debounce so active editing does not flood the pipeline.

## Implementation details

This source uses a two-layer runtime model:

1. **Raw sync layer (fast, reliable cursor loop)**
   - Reads change pages from Google Drive using the saved cursor.
   - Processes all pages in order.
   - Updates the source cursor only after the full page chain succeeds.
   - This protects cursor integrity and avoids data loss from partial processing.

2. **Classification + debounce layer (meaningful events)**
   - Loads the previous file snapshot from KV.
   - Compares old and current metadata to classify transitions:
     - created
     - moved
     - trashed / untrashed
     - shared-with-you
     - removed
   - For update-like changes, marks the file as pending and waits for a quiet window.
   - Flushes one `google.drive.file_updated` event when:
     - no new changes were seen for `update_quiet_window`, or
     - `update_max_session` is reached (safety flush).

### Why debounce exists

Google Docs and similar files can generate many small updates during active editing. Without debounce, one editing session can create noisy event bursts. Debounce collapses these into cleaner, more useful update events.

### Initial Synchronization (Bootstrapping)

When you first connect a Google Drive source, it needs to know about your existing files so it doesn't report them all as "newly created" the first time they change.

The `bootstrap_mode` setting controls this behavior:
- **`baseline_only` (Recommended)**: The source performs a quick crawl of your Drive to record the current state of all files. No events are emitted during this phase. Future changes will be compared against this baseline.
- **`full_snapshot`**: Similar to `baseline_only`, but also fetches and caches the text content of documents. This allows the very first update event after bootstrapping to include a text diff. This process is slower and uses more API quota.
- **`off`**: No initial crawl. The source will only start tracking changes from the moment it is first run. **Note**: This will cause all existing files to emit a `file_created` event the first time they are modified after the source starts.

### Authentication and Scopes

To fetch the content of files (required for `snippetBefore` and `snippetAfter` in events), the source requires the `https://www.googleapis.com/auth/drive.readonly` scope. 

When using the CLI to authorize, ensure you use the `drive` alias (which now points to the full readonly scope) rather than `drive_metadata`:
```bash
python main.py google auth --credentials-file data/credentials.json --scopes "drive,gmail,calendar" --token "data/google_token.json"
```

If you only want to track metadata changes (names, moves) and don't need text diffs, you can use `drive_metadata` scope and disable content fetching in `config.yaml` by setting `eligible_mime_types_for_content_diff: []`.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_drive:
    type: "google_drive"
    token_file: "data/google_token.json"
    poll_interval: "30s"
```

### Full Configuration

```yaml
sources:
  my_drive:
    type: "google_drive"
    token_file: "data/google_token.json"
    poll_interval: "30s"
    bootstrap_mode: "baseline_only"
    restrict_to_my_drive: false
    include_removed: true
    include_corpus_removals: false
    update_quiet_window: "60s"
    update_max_session: "10m"
```

### Configuration options explained

| Option                                  | What it controls                                                    | Practical guidance                                                                                          |
|:----------------------------------------|:--------------------------------------------------------------------|:------------------------------------------------------------------------------------------------------------|
| `token_file`                            | Path to OAuth token JSON used to call Google APIs.                  | Use a separate token file per environment (dev/stage/prod) to avoid credential mixups.                      |
| `poll_interval`                         | How often this source checks Google Drive for new changes.          | Supports human-readable values like `"30s"`, `"2m"`, `"10m"`. Shorter = fresher events, higher API usage.   |
| `restrict_to_my_drive`                  | Corpus scope preference.                                            | `true` keeps scope focused on My Drive. `false` allows wider visibility where available.                    |
| `include_removed`                       | Includes removal entries from Drive changes feed.                   | Usually keep enabled for auditability and troubleshooting.                                                  |
| `include_corpus_removals`               | Requests corpus-removal details when available.                     | Enable when you need more visibility into “removed from corpus” cases.                                      |
| `bootstrap_mode`                         | How to handle initial synchronization when first starting.          | `baseline_only` (default): fast, avoids "created" noise. `full_snapshot`: also caches text content. `off`: start fresh from now. |
| `update_quiet_window`                   | Quiet period before emitting debounced `google.drive.file_updated`. | If new edits arrive before this window ends, flush is delayed to avoid noisy update bursts.                 |
| `update_max_session`                    | Maximum wait time before forcing an update flush.                   | Prevents heavily edited files from staying pending forever.                                                 |
| `eligible_mime_types_for_content_diff`  | List of MIME types eligible for paragraph-level text diffing.       | Defaults to Google Docs and standard `text/*` files.                                                        |
| `max_diffable_file_bytes`               | Size limit for content fetching and diffing.                        | Prevents memory issues with giant text files. Default is usually 5-10MB.                                    |

## How to choose configuration values (real-life examples)

### Example A: Small team, mostly office docs
- Goal: near-real-time visibility without noise.
- Suggested values:
  - `poll_interval: "30s"`
  - `update_quiet_window: "60s"`
  - `update_max_session: "10m"`
  - `include_removed: true`

Why: fast polling keeps latency low, while 60s quiet window collapses active edits into cleaner updates.

### Example B: Cost-sensitive workload, low urgency
- Goal: reduce API calls and event volume.
- Suggested values:
  - `poll_interval: "5m"`
  - `update_quiet_window: "2m"`
  - `update_max_session: "15m"`

Why: fewer polling cycles reduce operational load and API usage.

### Example C: Compliance/audit-oriented pipeline
- Goal: preserve maximum visibility for investigations.
- Suggested values:
  - `poll_interval: "30s"`
  - `include_removed: true`
  - `include_corpus_removals: true`
  - `update_quiet_window: "45s"`
  - `update_max_session: "10m"`

Why: this keeps removal visibility while still collapsing noisy edit bursts into one update session event.

### Example D: Personal-drive-only ingestion
- Goal: avoid shared-drive complexity.
- Suggested values:
  - `restrict_to_my_drive: true`

Why: narrows scope and makes behavior easier to reason about in single-user setups.

## Event Definitions

| Type | Entity ID | Description |
| :--- | :--- | :--- |
| `google.drive.file_created` | Drive file ID | File first seen in local snapshot cache. |
| `google.drive.file_moved` | Drive file ID | Parent folder changed. |
| `google.drive.file_trashed` | Drive file ID | `trashed` changed `false -> true`. |
| `google.drive.file_untrashed` | Drive file ID | `trashed` changed `true -> false`. |
| `google.drive.file_shared_with_you` | Drive file ID | `sharedWithMeTime` appeared/advanced for non-owned file. |
| `google.drive.file_removed` | Drive file ID | Raw `change.removed=true` event. |
| `google.drive.file_updated` | Drive file ID | Debounced update after version/modifiedTime signal. |

## Emitted event examples

### Example: `google.drive.file_created`

```json
{
  "event_id": "drive-1AbCd-file_created-1741999501",
  "event_type": "google.drive.file_created",
  "entity_id": "1AbCd",
  "occurred_at": "2026-03-15T00:45:01Z",
  "data": {
    "fileId": "1AbCd",
    "name": "Q1 plan",
    "mimeType": "application/vnd.google-apps.document",
    "parentIds": ["0AExampleFolder"],
    "createdTime": "2026-03-15T00:40:10Z",
    "description": "Quarterly roadmap",
    "lastModifyingUser": {
      "displayName": "Alice",
      "emailAddress": "alice@example.com"
    }
  }
}
```

### Example: `google.drive.file_updated` (debounced)

```json
{
  "event_id": "drive-1AbCd-google.drive.file_updated-27",
  "event_type": "google.drive.file_updated",
  "entity_id": "1AbCd",
  "occurred_at": "2026-03-15T00:48:10Z",
  "data": {
    "fileId": "1AbCd",
    "name": "Q1 plan",
    "mimeType": "application/vnd.google-apps.document",
    "parentIds": ["0AExampleFolder"],
    "previousVersion": "21",
    "currentVersion": "27",
    "sessionStartedAt": "2026-03-15T00:46:12Z",
    "lastChangeSeenAt": "2026-03-15T00:47:56Z",
    "rawChangeCount": 4,
    "lastModifyingUser": {
      "displayName": "Alice",
      "emailAddress": "alice@example.com"
    },
    "snippetBefore": "Old paragraph content...",
    "snippetAfter": "New paragraph content...",
    "changedBlockCount": 1,
    "addedCharCount": 15,
    "removedCharCount": 10
  }
}
```

### Example: `google.drive.file_removed`

```json
{
  "event_id": "drive-1AbCd-google.drive.file_removed-2026-03-15T00:51:22Z",
  "event_type": "google.drive.file_removed",
  "entity_id": "1AbCd",
  "occurred_at": "2026-03-15T00:51:22Z",
  "data": {
    "fileId": "1AbCd",
    "lastKnownName": "Q1 plan",
    "lastKnownMimeType": "application/vnd.google-apps.document",
    "lastKnownParentIds": ["0AExampleFolder"]
  }
}
```

### Example: `google.drive.file_moved`

```json
{
  "event_id": "drive-1AbCd-google.drive.file_moved-2026-03-15T00:50:01Z",
  "event_type": "google.drive.file_moved",
  "entity_id": "1AbCd",
  "occurred_at": "2026-03-15T00:50:01Z",
  "data": {
    "fileId": "1AbCd",
    "name": "Q1 plan",
    "mimeType": "application/vnd.google-apps.document",
    "parentIds": ["0ANewFolder"],
    "parentIdsBefore": ["0AExampleFolder"],
    "parentIdsAfter": ["0ANewFolder"],
    "lastModifyingUser": {
      "displayName": "Alice",
      "emailAddress": "alice@example.com"
    }
  }
}
```

### Example: `google.drive.file_shared_with_you`

```json
{
  "event_id": "drive-7XyZa-google.drive.file_shared_with_you-2026-03-15T00:52:09Z",
  "event_type": "google.drive.file_shared_with_you",
  "entity_id": "7XyZa",
  "occurred_at": "2026-03-15T00:52:09Z",
  "data": {
    "fileId": "7XyZa",
    "name": "Vendor Contract",
    "mimeType": "application/pdf",
    "sharedWithMeTime": "2026-03-15T00:52:00Z",
    "sharingUser": {
      "displayName": "Alice",
      "emailAddress": "alice@example.com"
    }
  }
}
```

## Data payload reference

The `data` object contains event-specific deltas, not the full Drive `file` object.

- Common fields: `fileId`, `name`, `mimeType`.
- `file_created`: adds `parentIds`, `createdTime`.
- `file_moved`: adds `parentIdsBefore`, `parentIdsAfter`.
- `file_trashed` / `file_untrashed`: add `trashedBefore`, `trashedAfter`.
- `file_shared_with_you`: adds `sharedWithMeTime`, `sharingUser`.
- `file_removed`: adds `lastKnownName`, `lastKnownMimeType`, `lastKnownParentIds` from cache.
- `file_updated`: adds `previousVersion`, `currentVersion`, `sessionStartedAt`, `lastChangeSeenAt`, `rawChangeCount`.
  - For text files, also includes: `snippetBefore`, `snippetAfter`, `changedBlockCount`, `addedCharCount`, `removedCharCount`.

> `google.drive.file_deleted` and `google.drive.file_permission_changed` are intentionally not emitted in polling v1.
