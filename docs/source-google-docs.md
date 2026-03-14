# Google Docs Source

The Google Docs source tracks recent changes to your Google Docs files by polling for modified files.

## Pros & Cons

### Pros
- Automatically finds all Google Docs files modified in your Drive.
- Provides document ID, name, and modification timestamp.
- No per-document configuration required.

### Cons
- Polling-based (default 10m).
- Relies on the Drive API search functionality.
- Does not provide incremental *diffs* of content change (only the fact that a change happened).

## Core Concepts

- **Cursors (Last Modification Time)**: We store the timestamp of the latest modified document encountered. On the next poll, we search for files modified *after* that timestamp.
- **Event ID**: The internal event ID is a combination of the document ID, modification time, and version number.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_docs:
    type: "google_docs"
    token_file: "data/google_token.json"
```

### Full Configuration

```yaml
sources:
  my_docs:
    type: "google_docs"
    token_file: "data/google_token.json"
    poll_interval: "10m"
```

## Events

- **Type**: `docs.document_change`
- **Entity ID**: Google Document/File ID
- **Data**:
    - `id`: The ID of the document.
    - `name`: Name of the document.
    - `modifiedTime`: RFC 3339 timestamp of the last modification.
    - `version`: The version number of the document.
