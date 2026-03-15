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
    bootstrap_mode: "baseline_only" # baseline_only | full_snapshot
    track_shared_drives: false
    restrict_to_my_drive: false
    include_removed: true
    include_corpus_removals: false
    update_quiet_window: "60s"
    update_max_session: "10m"
    emit_file_removed: true
    emit_file_deleted_only_when_confirmed: true # reserved for Drive Activity enrichment phase
    emit_permission_changed: true # reserved for Drive Activity/permission-diff enrichment phase
```

### Configuration options explained

| Option                                  | What it controls                                                    | Practical guidance                                                                                          |
|:----------------------------------------|:--------------------------------------------------------------------|:------------------------------------------------------------------------------------------------------------|
| `token_file`                            | Path to OAuth token JSON used to call Google APIs.                  | Use a separate token file per environment (dev/stage/prod) to avoid credential mixups.                      |
| `poll_interval`                         | How often this source checks Google Drive for new changes.          | Supports human-readable values like `"30s"`, `"2m"`, `"10m"`. Shorter = fresher events, higher API usage.   |
| `bootstrap_mode`                        | Initial startup strategy.                                           | `baseline_only` starts from now (future-only). `full_snapshot` is for initial inventory/backfill workflows. |
| `track_shared_drives`                   | Whether shared-drive tracking behavior is enabled.                  | Keep `false` if your use case is only personal My Drive content.                                            |
| `restrict_to_my_drive`                  | Corpus scope preference.                                            | `true` keeps scope focused on My Drive. `false` allows wider visibility where available.                    |
| `include_removed`                       | Includes removal entries from Drive changes feed.                   | Usually keep enabled for auditability and troubleshooting.                                                  |
| `include_corpus_removals`               | Requests corpus-removal details when available.                     | Enable when you need more visibility into “removed from corpus” cases.                                      |
| `update_quiet_window`                   | Quiet period before emitting debounced `google.drive.file_updated`. | If new edits arrive before this window ends, flush is delayed to avoid noisy update bursts.                 |
| `update_max_session`                    | Maximum wait time before forcing an update flush.                   | Prevents heavily edited files from staying pending forever.                                                 |
| `eligible_mime_types_for_content_diff`  | List of MIME types eligible for paragraph-level text diffing.       | Defaults to Google Docs and standard `text/*` files.                                                        |
| `max_diffable_file_bytes`               | Size limit for content fetching and diffing.                        | Prevents memory issues with giant text files. Default is usually 5-10MB.                                    |
| `emit_file_removed`                     | Enables/disables `google.drive.file_removed` emission.              | Recommended `true` for observability of raw removal signals.                                                |
| `emit_file_deleted_only_when_confirmed` | Conservative deletion policy switch.                                | Keep `true` if you do not want to treat every removal as a confirmed delete.                                |
| `emit_permission_changed`               | Reserved for future enrichment-based permission events.             | Polling v1 does not emit `google.drive.file_permission_changed`; keep this as a forward-compatible toggle.  |

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
  - `emit_file_removed: true`
  - `update_quiet_window: "45s"`
  - `update_max_session: "10m"`

Why: this keeps removal visibility while still collapsing noisy edit bursts into one update session event.

### Example D: Personal-drive-only ingestion
- Goal: avoid shared-drive complexity.
- Suggested values:
  - `track_shared_drives: false`
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
    "createdTime": "2026-03-15T00:40:10Z"
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
    "previousVersion": "21",
    "currentVersion": "27",
    "sessionStartedAt": "2026-03-15T00:46:12Z",
    "lastChangeSeenAt": "2026-03-15T00:47:56Z",
    "rawChangeCount": 4,
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
    "parentIdsBefore": ["0AExampleFolder"],
    "parentIdsAfter": ["0ANewFolder"]
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

> `google.drive.file_deleted` and `google.drive.file_permission_changed` are intentionally not emitted in polling v1. They should be added only after Drive Activity or permission-snapshot enrichment is implemented.
