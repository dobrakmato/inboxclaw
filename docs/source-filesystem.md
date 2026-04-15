# Filesystem Source

The Filesystem source watches a local directory for file changes and emits events when files are created, updated, deleted, or moved. It is a **generic building block** — useful on its own for any directory of files, and the foundation for app-specific sources like [Logseq](#logseq-setup) and [Obsidian](#obsidian-setup).

Instead of relying solely on periodic scanning (which wastes CPU) or solely on OS-level file watchers (which miss changes during downtime), this source uses a **hybrid approach** by default: real-time detection via [watchdog](https://github.com/gorakhargosh/watchdog) combined with periodic reconciliation scans as a safety net.

## When to Use

- You want to track changes in a local directory (notes, documents, code, exports, etc.).
- You use a Markdown-based knowledge tool like **Logseq**, **Obsidian**, **Dendron**, or **Foam**.
- You want near-instant event detection with the reliability of periodic polling as a fallback.

## Getting Started

Add a Filesystem source to your `config.yaml`:

```yaml
sources:
  my_notes:
    type: filesystem
    path: "/home/user/Documents/notes"
```

This will watch the directory recursively using hybrid mode (watchdog + reconciliation every 5 minutes), tracking all file types.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_notes:
    type: filesystem
    path: "/home/user/Documents/notes"
```

Defaults: `watch_mode: "hybrid"`, `poll_interval: "5m"`, all file types, recursive, no content preview.

### Full Configuration

```yaml
sources:
  my_notes:
    type: filesystem
    path: "/home/user/Documents/notes"
    watch_mode: "hybrid"              # "watch", "poll", or "hybrid" (default)
    poll_interval: "5m"               # reconciliation interval (hybrid/poll modes)
    extensions:                       # file extensions to watch (default: all)
      - ".md"
      - ".org"
      - ".txt"
    ignore_patterns:                  # glob patterns to skip
      - ".git/**"
      - "*.tmp"
      - ".trash/**"
    recursive: true                   # watch subdirectories (default: true)
    include_content_preview: true     # include first N chars in event data
    content_preview_length: 200       # preview length (default: 200)
    max_changed_sections: 5           # max sections in content_diff (default: 5)
    max_section_chars: 300            # max chars per diff section (default: 300)
    coalesce:
      - match: "fs.file_updated"
        strategy: debounce
        window: "3s"                  # debounce rapid saves
```

### Configuration Reference

| Parameter                | Type           | Default                              | Description                                                                                          |
|:-------------------------|:---------------|:-------------------------------------|:-----------------------------------------------------------------------------------------------------|
| `path`                   | `string`       | *(required)*                         | Absolute path to the directory to watch.                                                             |
| `watch_mode`             | `string`       | `"hybrid"`                           | Detection mode: `"watch"` (watchdog only), `"poll"` (scan only), or `"hybrid"` (both).               |
| `poll_interval`          | `string`       | `"5m"`                               | How often to run a full reconciliation scan. Used in `hybrid` and `poll` modes.                       |
| `extensions`             | `list[string]` | `null` (all files)                   | File extensions to include (e.g. `[".md", ".org"]`). If not set, all files are tracked.              |
| `ignore_patterns`        | `list[string]` | `[".git/**", "*.tmp", ".trash/**"]`  | Glob patterns for files/directories to skip.                                                         |
| `recursive`              | `bool`         | `true`                               | Whether to watch subdirectories.                                                                     |
| `include_content_preview`| `bool`         | `false`                              | Include the first N characters of text file content in event data. Ignored for binary files.          |
| `content_preview_length` | `int`          | `200`                                | Number of characters to include in the content preview.                                              |
| `max_changed_sections`   | `int`          | `5`                                  | Maximum number of changed sections to include in `content_diff` for text file updates.               |
| `max_section_chars`      | `int`          | `300`                                | Maximum characters per section in `content_diff`.                                                    |

### Watch Modes Explained

| Mode     | How it works                                                                 | Best for                                    |
|:---------|:-----------------------------------------------------------------------------|:--------------------------------------------|
| `hybrid` | Watchdog for real-time detection + periodic full scan as safety net.          | Most use cases. Best balance of speed and reliability. |
| `watch`  | Watchdog only. No periodic scanning.                                         | When you need minimal CPU usage and can tolerate missed events on restart. |
| `poll`   | Periodic full directory scan only. No watchdog.                              | Network drives, FUSE mounts, or environments where watchdog doesn't work. |

**Why hybrid is the default:** OS-level file watchers (`inotify`, `FSEvents`, `ReadDirectoryChangesW`) are fast but miss changes that happen while the process is down. The periodic reconciliation scan catches those gaps. The coalescer prevents duplicate events when both the watcher and the scan detect the same change.

## Event Definitions

| Type              | Entity ID          | Description                                      |
|:------------------|:-------------------|:-------------------------------------------------|
| `fs.file_created` | Relative file path | A new file appeared in the watched directory.     |
| `fs.file_updated` | Relative file path | An existing file's content changed.               |
| `fs.file_deleted` | Relative file path | A file was removed.                               |
| `fs.file_moved`   | New relative path  | A file was renamed or moved within the directory. |

### Event Example

`fs.file_created` when a new Markdown file is added:

```json
{
  "id": 1,
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "event_type": "fs.file_created",
  "entity_id": "pages/Project Ideas.md",
  "created_at": "2026-04-15T12:00:00+00:00",
  "data": {
    "path": "pages/Project Ideas.md",
    "mime_type": "text/markdown",
    "size_bytes": 2048
  },
  "meta": {}
}
```

`fs.file_updated` — text file with content diff and preview:

```json
{
  "id": 2,
  "event_id": "660e8400-e29b-41d4-a716-446655440001",
  "event_type": "fs.file_updated",
  "entity_id": "pages/Project Ideas.md",
  "created_at": "2026-04-15T12:05:00+00:00",
  "data": {
    "path": "pages/Project Ideas.md",
    "mime_type": "text/markdown",
    "size_bytes": 2560,
    "content_preview": "# Project Ideas\n\n## Q2 Goals\n- Launch new feature...",
    "content_diff": {
      "totalChangedSections": 2,
      "changes": [
        {
          "before": "## Q2 Goals\n- Draft proposal",
          "after": "## Q2 Goals\n- Launch new feature\n- Write docs"
        }
      ],
      "addedCharCount": 45,
      "removedCharCount": 18
    }
  },
  "meta": {}
}
```

`fs.file_updated` — binary file (metadata only, no diff or preview):

```json
{
  "id": 3,
  "event_id": "770e8400-e29b-41d4-a716-446655440003",
  "event_type": "fs.file_updated",
  "entity_id": "assets/diagram.png",
  "created_at": "2026-04-15T12:06:00+00:00",
  "data": {
    "path": "assets/diagram.png",
    "mime_type": "image/png",
    "size_bytes": 51200
  },
  "meta": {}
}
```

`fs.file_moved` when a file is renamed:

```json
{
  "id": 4,
  "event_id": "880e8400-e29b-41d4-a716-446655440002",
  "event_type": "fs.file_moved",
  "entity_id": "pages/Renamed Ideas.md",
  "created_at": "2026-04-15T12:10:00+00:00",
  "data": {
    "path": "pages/Renamed Ideas.md",
    "mime_type": "text/markdown",
    "size_bytes": 2560,
    "old_path": "pages/Project Ideas.md"
  },
  "meta": {}
}
```

## Coalescing

Text editors and note-taking apps often trigger many rapid saves. **Coalescing is strongly recommended** to debounce `fs.file_updated` events and avoid noise.

```yaml
sources:
  my_notes:
    type: filesystem
    path: "/home/user/notes"
    coalesce:
      - match: "fs.file_updated"
        strategy: debounce
        window: "3s"
```

This means: if a file is saved 5 times in 2 seconds, only the final state is emitted as a single event after 3 seconds of quiet.

See the [Event Coalescing](coalescing.md) page for more details.

## Logseq Setup

[Logseq](https://logseq.com/) is a local-first knowledge base that stores pages and journals as Markdown (`.md`) or Org-mode (`.org`) files in a **graph directory**.

A typical Logseq graph looks like:

```
my-graph/
├── pages/          # Named pages
├── journals/       # Daily journal entries (e.g. 2026_04_15.md)
├── assets/         # Attached images, PDFs
└── logseq/         # Internal config (should be ignored)
```

### Logseq Configuration

```yaml
sources:
  my_logseq:
    type: filesystem
    path: "/home/user/Documents/my-graph"
    extensions:
      - ".md"
      - ".org"
    ignore_patterns:
      - "logseq/**"
      - ".git/**"
      - "assets/**"
      - "*.tmp"
    include_content_preview: true
    content_preview_length: 200
    coalesce:
      - match: "fs.file_updated"
        strategy: debounce
        window: "5s"
```

This watches only Markdown and Org-mode files, ignores Logseq's internal config directory and assets, and debounces rapid saves with a 5-second window.

### What You'll Get

- `fs.file_created` when you create a new page or journal entry.
- `fs.file_updated` when you edit an existing page (debounced).
- `fs.file_deleted` when you delete a page.
- `fs.file_moved` when you rename a page.

The `entity_id` will be the relative path within the graph, e.g. `pages/Project Ideas.md` or `journals/2026_04_15.md`, making it easy to match on page type:

```yaml
sink:
  journal_webhook:
    type: webhook
    url: "https://example.com/hook"
    match: "fs.file_created"
    # Use a command sink with filtering to act only on journal entries
```

## Obsidian Setup

[Obsidian](https://obsidian.md/) is a Markdown-based knowledge base that stores notes in a **vault directory**. Notes can be organized in any folder structure.

A typical Obsidian vault looks like:

```
my-vault/
├── Daily Notes/        # Journal entries (configurable)
├── Projects/           # Organized folders
├── Inbox/              # Quick capture
├── .obsidian/          # Internal config (should be ignored)
└── .trash/             # Obsidian's trash (should be ignored)
```

### Obsidian Configuration

```yaml
sources:
  my_obsidian:
    type: filesystem
    path: "/home/user/Documents/my-vault"
    extensions:
      - ".md"
    ignore_patterns:
      - ".obsidian/**"
      - ".git/**"
      - ".trash/**"
      - "*.tmp"
    include_content_preview: true
    content_preview_length: 200
    coalesce:
      - match: "fs.file_updated"
        strategy: debounce
        window: "3s"
```

This watches only Markdown files, ignores Obsidian's internal directories and trash, and debounces rapid saves.

### What You'll Get

- `fs.file_created` when you create a new note.
- `fs.file_updated` when you edit a note (debounced).
- `fs.file_deleted` when you delete a note (or move it to `.trash/`).
- `fs.file_moved` when you rename or reorganize notes.

The `entity_id` will be the relative path within the vault, e.g. `Projects/Q2 Goals.md` or `Daily Notes/2026-04-15.md`.

### Obsidian on Multiple Devices

If you sync your Obsidian vault across devices (e.g. via Syncthing, iCloud, or Obsidian Sync), the filesystem source on each device will independently detect changes. To avoid duplicate processing, run Inboxclaw on only one device, or use distinct source names per device and handle deduplication downstream.
