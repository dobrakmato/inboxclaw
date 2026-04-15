# Jira Source

The Jira source monitors a Jira Cloud instance for tasks assigned to the current user (or any custom JQL query) and emits events when tasks are assigned, updated, or unassigned. It maintains a local cache of task states to detect changes and provides detailed diffs of what changed.

Use this source to build workflows that react to Jira activity: sync tasks to a local TODO list, notify on status changes, trigger automations on reassignment, or track comments.

## Getting Started

### 1. Obtain an API Token

1. Log in to your Atlassian account.
2. Go to **Account Settings** > **Security** > **Create and manage API tokens**.
3. Create a new token and copy it.

### 2. Add the source to `config.yaml`

```yaml
sources:
  my_jira:
    type: jira
    url: "https://your-domain.atlassian.net"
    email: "your-email@example.com"
    api_token: "${JIRA_API_TOKEN}" # Recommended to use an environment variable
```

## Core Concepts

### Polling and Dirty Detection

The Jira source periodically executes a JQL query (default: `assignee = currentUser() AND resolution = Unresolved`). It compares the results with a local snapshot stored in the database.

- **New Key**: If an issue appears in the results that wasn't there before, a `jira.task_assigned` event is emitted.
- **Missing Key**: If an issue that was in the previous snapshot is no longer in the JQL results, a `jira.task_unassigned` event is emitted.
- **Updated Timestamp**: If an issue's `updated` field has changed, the source fetches the full issue details, changelog, and comments, then classifies the changes into specific event types.

### Event Classification

When an update is detected, the source inspects the changes and emits **separate, specific events** instead of a single generic update. A single poll cycle can emit multiple events for the same issue:

- **Status changes** → `jira.task_status_changed`
- **Assignee changes** → `jira.task_reassigned`
- **New comments** → `jira.comment_added` (one per new comment)
- **Edited comments** → `jira.comment_updated` (one per edited comment)
- **Other field changes** → `jira.task_updated` (everything not covered above)

This separation makes downstream consumers much simpler — you can react to status transitions without parsing a generic diff blob.

### Field Discovery

Jira instances often have many custom fields (e.g., `customfield_10001`). On startup and every 24 hours, the source fetches the full field catalog from Jira. This allows it to map technical field IDs to human-readable names (e.g., "Sprint", "Story Points") in the emitted events.

### Diffing and Ignored Fields

When an update is detected, the source compares all fields (except those in `ignored_fields`). The resulting `diff` in the event data contains `before` and `after` values for each changed field.

**Important**: The `ignored_fields` list affects:
1. **Update Detection**: Changes in these fields won't trigger a `jira.task_updated` event.
2. **Diff Generation**: These fields will be excluded from the `diff` object in the event data.
3. **Changelog Filtering**: Any items in the `changelog` that refer to these fields will be removed. If a changelog entry has no items left after filtering, the entire entry is omitted.

The `full_issue` and `last_known_state` objects still contain the raw data from Jira, including ignored fields, for completeness.

### Comment Tracking

Comments are tracked separately from issue fields. The source fetches comments via the Jira comment API and caches them alongside the issue state. On each poll:

- **New comment IDs** (not in the previous cache) → `jira.comment_added`
- **Same comment ID but different `updated` timestamp** → `jira.comment_updated`
- **Unchanged comments** → no event

This approach is more robust than relying on the changelog, as it gives full comment bodies and handles multiple comments added between polls.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_jira:
    type: jira
    url: "https://mycompany.atlassian.net"
    email: "me@mycompany.com"
    api_token: "..." 
```

### Full Configuration

```yaml
sources:
  my_jira:
    type: jira
    url: "https://mycompany.atlassian.net"
    email: "me@mycompany.com"
    api_token: "${JIRA_API_TOKEN}"
    jql: "project = PROJ AND assignee = currentUser() ORDER BY updated DESC"
    poll_interval: "5m"
    field_discovery_interval: "1d"
    ignored_fields:
      - updated
      - lastViewed
      - workratio
```

### Configuration Reference

| Parameter                  | Type     | Default                                                                 | Description                                                                                                   |
|:---------------------------|:---------|:------------------------------------------------------------------------|:--------------------------------------------------------------------------------------------------------------|
| `url`                      | `string` | Required                                                                | The base URL of your Jira Cloud instance (e.g., `https://site.atlassian.net`).                                |
| `email`                    | `string` | Env var                                                                 | Your Atlassian account email. Defaults to `JIRA_EMAIL` env var.                                               |
| `api_token`                | `string` | Env var                                                                 | Your Jira API token. Defaults to `JIRA_API_TOKEN` env var.                                                    |
| `jql`                      | `string` | `assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC` | The JQL query used to discover tasks.                                                                         |
| `poll_interval`            | `string` | `"1m"`                                                                  | How often to poll for changes. Supports human-readable intervals (e.g. `"5m"`, `"30s"`).                       |
| `field_discovery_interval` | `string` | `"24h"`                                                                 | How often to refresh the field name mapping.                                                                  |
| `ignored_fields`           | `list`   | `["updated", "workratio", "lastViewed", "aggregateprogress", "progress"]` | List of Jira field IDs to ignore when detecting updates. Changes in these fields won't trigger an update event. |

## Event Definitions

| Type                       | Entity ID | Description                                                        |
|:---------------------------|:----------|:-------------------------------------------------------------------|
| `jira.task_assigned`       | Issue Key | A task now matches your JQL (e.g., assigned to you).               |
| `jira.task_unassigned`     | Issue Key | A task no longer matches your JQL (e.g., unassigned).              |
| `jira.task_updated`        | Issue Key | Generic field changes (everything not covered by specific events). |
| `jira.task_status_changed` | Issue Key | Status transition (e.g., To Do → In Progress).                     |
| `jira.task_reassigned`     | Issue Key | Assignee changed while task still matches JQL.                     |
| `jira.comment_added`       | Issue Key | A new comment was added on a tracked task.                         |
| `jira.comment_updated`     | Issue Key | An existing comment was edited on a tracked task.                  |

## Event Examples

### `jira.task_assigned`

```json
{
  "event_id": "jira-PROJ-123-2024-03-15T10:00:00.000+0000",
  "event_type": "jira.task_assigned",
  "entity_id": "PROJ-123",
  "data": {
    "issue_key": "PROJ-123",
    "summary": "Fix login bug",
    "status": "To Do",
    "priority": "High",
    "issue_type": "Bug",
    "project": "Platform Support",
    "full_issue": {...}
  }
}
```

### `jira.task_unassigned`

```json
{
  "event_id": "jira-PROJ-123-unassigned-2024-03-15T12:00:00.000+0000",
  "event_type": "jira.task_unassigned",
  "entity_id": "PROJ-123",
  "data": {
    "issue_key": "PROJ-123",
    "summary": "Fix login bug",
    "status": "In Progress",
    "assignee": "John Doe",
    "priority": "High",
    "issuetype": "Bug",
    "project": "Platform Support",
    "last_known_state": {...}
  }
}
```

### `jira.task_status_changed`

```json
{
  "event_id": "jira-PROJ-123-status-2024-03-15T10:00:00.000+0000",
  "event_type": "jira.task_status_changed",
  "entity_id": "PROJ-123",
  "data": {
    "issue_key": "PROJ-123",
    "summary": "Fix login bug",
    "status_before": "To Do",
    "status_after": "In Progress",
    "changelog": [...],
    "full_issue": {...}
  }
}
```

### `jira.task_reassigned`

```json
{
  "event_id": "jira-PROJ-123-reassigned-2024-03-15T10:00:00.000+0000",
  "event_type": "jira.task_reassigned",
  "entity_id": "PROJ-123",
  "data": {
    "issue_key": "PROJ-123",
    "summary": "Fix login bug",
    "assignee_before": "Alice",
    "assignee_after": "Bob",
    "changelog": [...],
    "full_issue": {...}
  }
}
```

### `jira.task_updated`

```json
{
  "event_id": "jira-PROJ-123-2024-03-15T10:00:00.000+0000",
  "event_type": "jira.task_updated",
  "entity_id": "PROJ-123",
  "data": {
    "issue_key": "PROJ-123",
    "summary": "Fix login bug",
    "diff": {
      "Description": {
        "field_id": "description",
        "before": null,
        "after": "Detailed bug report..."
      },
      "Story Points": {
        "field_id": "customfield_10001",
        "before": null,
        "after": 5
      }
    },
    "changelog": [...],
    "full_issue": {...}
  }
}
```

### `jira.comment_added`

```json
{
  "event_id": "jira-PROJ-123-comment-10042-created",
  "event_type": "jira.comment_added",
  "entity_id": "PROJ-123",
  "data": {
    "issue_key": "PROJ-123",
    "summary": "Fix login bug",
    "comment_id": "10042",
    "author": "Jane Doe",
    "body": "I've reproduced this on staging.",
    "created": "2024-03-15T10:30:00.000+0000"
  }
}
```

### `jira.comment_updated`

```json
{
  "event_id": "jira-PROJ-123-comment-10042-updated-2024-03-15T11:00:00.000+0000",
  "event_type": "jira.comment_updated",
  "entity_id": "PROJ-123",
  "data": {
    "issue_key": "PROJ-123",
    "summary": "Fix login bug",
    "comment_id": "10042",
    "author": "Jane Doe",
    "body_before": "I've reproduced this on staging.",
    "body_after": "I've reproduced this on staging. Logs attached.",
    "updated": "2024-03-15T11:00:00.000+0000"
  }
}
```
