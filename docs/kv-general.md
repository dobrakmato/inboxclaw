# Key/Value Storage for Sources

The Ingest Pipeline provides a simple Key/Value (KV) storage for sources to store persistent data between polling cycles.

This is primarily used for synchronization state, such as cursors, sync tokens, or change markers. By storing this information in the database, sources can resume from where they left off even after a server restart.

## How it Works

Every source has its own isolated KV space within the pipeline's database. This isolation is managed by associating each KV pair with a unique `source_id`.

- **Persistent**: Data is stored in the database and survives application restarts.
- **Isolated**: Sources cannot access each other's KV pairs.
- **Generic**: Values are stored as JSON-serializable types (strings, numbers, booleans, lists, and dictionaries).

## Common Use Cases

### 1. Synchronization Cursors
Many APIs provide a "sync token" or a timestamp indicating the last time you fetched changes. Storing this token in the KV storage allows the source to only fetch new data on the next poll.

> [!NOTE]
> **Built-in Cursor Mechanism**: If your source only needs to track a single synchronization value (like a `sync_token` or `last_timestamp`), use the built-in **Source Cursor** instead of the KV storage. Every source has a dedicated `cursor` field in the database that is more efficient for this purpose. Use KV storage only when you need to track multiple independent cursors or more complex state.

*Example: The Gmail and Google Drive sources store their last `sync_token` or `start_page_token` to efficiently fetch changes.*

### 2. Delta Tracking
If an API doesn't support native sync tokens, a source can store the timestamp or ID of the last processed item to filter out old entries during the next poll.

## Clean Design & Maintenance

The `SourceKVService` provides methods for managing these entries, including:

- **Automatic Expiration**: Some sources use KV for temporary tracking and may clean up old entries based on a cutoff time or a key prefix.
- **Deduplication Support**: While the pipeline's core deduplicates events based on `event_id`, KV storage helps sources avoid even *fetching* the same data multiple times from the external API.

For implementation details, developers should refer to `src/pipeline/kv.py`.
