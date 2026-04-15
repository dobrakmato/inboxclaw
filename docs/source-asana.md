# Asana Source

The Asana source monitors one or more Asana projects for task changes and emits events when tasks are created, updated, assigned, unassigned, completed, removed, or commented on. It uses a polling approach with local caching to detect changes — the same pattern used by the Jira source.

Use this source to build workflows that react to Asana activity: sync tasks to a local system, notify on assignment changes, or track project progress.

## Getting Started

### 1. Create a Personal Access Token

1. Log in to Asana.
2. Go to **My Settings** → **Apps** → **Developer Apps** → **Personal Access Tokens** (or visit [https://app.asana.com/0/developer-console](https://app.asana.com/0/developer-console)).
3. Click **Create new token**, give it a name, and copy the token.

> **Tip**: Store the token in an environment variable (`ASANA_ACCESS_TOKEN`) rather than hardcoding it.

### 2. Find Your Project GID

Every Asana project has a unique GID. You can find it in the URL when viewing a project:

```
https://app.asana.com/0/1234567890/list
                         ^^^^^^^^^^
                         This is the project GID
```

Alternatively, use the Asana API:
```bash
curl -H "Authorization: Bearer $ASANA_ACCESS_TOKEN" \
  "https://app.asana.com/api/1.0/workspaces/{workspace_gid}/projects"
```

### 3. Add the Source to `config.yaml`

```yaml
sources:
  my_asana:
    type: asana
    access_token: "${ASANA_ACCESS_TOKEN}"
    project_gids:
      - "1234567890"
```

## Core Concepts

### Polling and Dirty Detection

The source periodically lists all tasks in each configured project. It compares the results with a local snapshot stored in the database:

- **New GID**: A task appears that wasn't there before → `asana.task_created` (and `asana.task_assigned` if it has an assignee).
- **Missing GID**: A task that was in the previous snapshot is gone → `asana.task_removed`.
- **Changed `modified_at`**: The source fetches full task details, computes a diff, and emits `asana.task_updated`. Within the diff, it also detects specific changes like assignee or completion status.

### Comment Tracking

When `track_comments` is enabled (default), the source polls the Stories API for each task to detect new comments. It maintains a list of known comment GIDs and emits `asana.task_commented` for any new ones.

### Field Discovery

Asana's built-in fields (name, assignee, due_on, etc.) are fixed and hardcoded. **Custom fields** are discovered per-project via the Custom Field Settings API and refreshed periodically (default: every 24 hours). Custom field changes appear in the diff with their human-readable names.

### Diffing and Ignored Fields

When an update is detected, the source compares built-in fields and custom fields (except those in `ignored_fields`). The `diff` in the event data contains `before` and `after` values for each changed field.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_asana:
    type: asana
    access_token: "${ASANA_ACCESS_TOKEN}"
    project_gids:
      - "1234567890"
```

### Full Configuration

```yaml
sources:
  my_asana:
    type: asana
    access_token: "${ASANA_ACCESS_TOKEN}"
    project_gids:
      - "1234567890"
      - "0987654321"
    poll_interval: "2m"
    field_discovery_interval: "1d"
    track_comments: true
    ignored_fields:
      - modified_at
      - liked
      - num_likes
      - num_subtasks
```

### Configuration Reference

| Parameter                  | Type       | Default                                              | Description                                                                 |
|:---------------------------|:-----------|:-----------------------------------------------------|:----------------------------------------------------------------------------|
| `access_token`             | `string`   | Env var `ASANA_ACCESS_TOKEN`                         | Your Asana Personal Access Token.                                           |
| `project_gids`             | `list`     | Required                                             | List of Asana project GIDs to monitor.                                      |
| `poll_interval`            | `string`   | `"1m"`                                               | How often to poll for changes. Supports human-readable intervals.           |
| `field_discovery_interval` | `string`   | `"24h"`                                              | How often to refresh the custom field mapping.                              |
| `track_comments`           | `bool`     | `true`                                               | Whether to poll for new comments on tasks.                                  |
| `ignored_fields`           | `list`     | `["modified_at", "liked", "num_likes", "num_subtasks"]` | Fields to exclude from diff computation and update detection.               |

## Event Definitions

| Type                    | Entity ID | Description                                                    |
|:------------------------|:----------|:---------------------------------------------------------------|
| `asana.task_created`    | Task GID  | A new task appeared in the project.                            |
| `asana.task_updated`    | Task GID  | A tracked task was modified (fields changed).                  |
| `asana.task_assigned`   | Task GID  | A task was assigned (or reassigned) to someone.                |
| `asana.task_unassigned` | Task GID  | A task's assignee was removed.                                 |
| `asana.task_completed`  | Task GID  | A task was marked as completed.                                |
| `asana.task_removed`    | Task GID  | A task is no longer in the project.                            |
| `asana.task_commented`  | Task GID  | A new comment was added to a task.                             |

## Event Examples

### `asana.task_created`

```json
{
  "event_id": "asana-12345-created-2024-03-20T10:00:00.000Z",
  "event_type": "asana.task_created",
  "entity_id": "12345",
  "data": {
    "task_gid": "12345",
    "name": "Design landing page",
    "assignee": "Alice",
    "completed": false,
    "project_gid": "1234567890",
    "full_task": { "..." : "..." }
  }
}
```

### `asana.task_updated`

```json
{
  "event_id": "asana-12345-updated-2024-03-22T15:00:00.000Z",
  "event_type": "asana.task_updated",
  "entity_id": "12345",
  "data": {
    "task_gid": "12345",
    "name": "Design landing page",
    "diff": {
      "name": {
        "before": "Design page",
        "after": "Design landing page"
      },
      "Priority Level": {
        "field_gid": "cf1",
        "before": "Medium",
        "after": "High"
      }
    },
    "full_task": { "..." : "..." }
  }
}
```

### `asana.task_commented`

```json
{
  "event_id": "asana-12345-comment-67890",
  "event_type": "asana.task_commented",
  "entity_id": "12345",
  "data": {
    "task_gid": "12345",
    "comment_gid": "67890",
    "text": "Looks great, let's ship it!",
    "author": "Bob",
    "project_gid": "1234567890"
  }
}
```

### `asana.task_removed`

```json
{
  "event_id": "asana-12345-removed-2024-03-25T10:00:00+00:00",
  "event_type": "asana.task_removed",
  "entity_id": "12345",
  "data": {
    "task_gid": "12345",
    "name": "Design landing page",
    "assignee": "Alice",
    "project_gid": "1234567890",
    "last_known_state": { "..." : "..." }
  }
}
```
