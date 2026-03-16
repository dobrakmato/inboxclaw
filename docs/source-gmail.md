# Gmail Source

The Gmail source monitors a Google mailbox and emits events when emails are received, sent, deleted, or when labels change. It uses the Gmail History API for efficient incremental sync — only new changes are fetched on each poll.

Use this source to build workflows that react to incoming emails: ticket creation, archiving, notifications based on sender or subject, etc.

## Getting Started

### 1. Authorize access

Generate a Google OAuth token with the `gmail` scope using the [Google Auth CLI](google-auth-cli.md):

```bash
python main.py google auth --scopes gmail --token data/google_token.json
```

### 2. Add the source to `config.yaml`

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
```

The source will start polling on startup. On the first run it initializes a `historyId` cursor from the most recent message and begins tracking changes from that point forward.

## Core Concepts

### Incremental Sync

The source uses Gmail's `history.list` API with a stored `historyId` cursor. Each poll fetches only the changes since the last cursor, making it efficient even for busy mailboxes. If the cursor expires (too old), the source re-initializes automatically.

### Label Filtering

By default, messages with the `SPAM` label are skipped. You can customize this with `exclude_label_ids` — any message that has at least one matching label is ignored.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
```

Defaults: `poll_interval: "10m"`, `exclude_label_ids: ["SPAM"]`.

### Full Configuration

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
    poll_interval: "1m"
    exclude_label_ids: ["SPAM", "TRASH", "CATEGORY_PROMOTIONS"]
```

### Configuration Reference

| Parameter           | Type     | Default      | Description                                                                                  |
|:--------------------|:---------|:-------------|:---------------------------------------------------------------------------------------------|
| `token_file`        | `string` | Required     | Path to the Google OAuth2 token file (created via [Google Auth CLI](google-auth-cli.md)).     |
| `poll_interval`     | `string` | `"10m"`      | How often to check for changes. Supports human-readable intervals (e.g. `"5m"`, `"30s"`).    |
| `exclude_label_ids` | `list`   | `["SPAM"]`   | Messages with any of these labels are skipped. Common labels: `SPAM`, `TRASH`, `UNREAD`, `STARRED`, `IMPORTANT`, `INBOX`, `CATEGORY_PERSONAL`, `CATEGORY_SOCIAL`, `CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, `CATEGORY_FORUMS`. |

## Event Definitions

| Type                    | Entity ID          | Description                                      |
|:------------------------|:-------------------|:-------------------------------------------------|
| `gmail.message_received`| Google Message ID  | A new email was received (not in SENT label).     |
| `gmail.message_sent`    | Google Message ID  | A new email was sent (has SENT label).            |
| `gmail.message_deleted` | Google Message ID  | An email was deleted.                             |
| `gmail.label_added`     | Google Message ID  | One or more labels were added to an email.        |
| `gmail.label_removed`   | Google Message ID  | One or more labels were removed from an email.    |

### Event Examples

#### `gmail.message_received` / `gmail.message_sent`

```json
{
  "id": 10,
  "event_id": "msg_abc123",
  "event_type": "gmail.message_received",
  "entity_id": "msg_abc123",
  "created_at": "2024-03-15T10:00:00+00:00",
  "data": {
    "threadId": "thread_xyz",
    "messageId": "msg_abc123",
    "snippet": "Brief preview of the email content...",
    "from": "Sender Name <sender@example.com>",
    "to": "Recipient <recipient@example.com>",
    "subject": "Email Subject",
    "date": "Sat, 15 Mar 2024 10:00:00 +0000",
    "labelIds": ["INBOX", "UNREAD"]
  },
  "meta": {}
}
```

#### `gmail.message_deleted`

```json
{
  "id": 11,
  "event_id": "msg_abc123-deleted",
  "event_type": "gmail.message_deleted",
  "entity_id": "msg_abc123",
  "created_at": "2024-03-15T10:05:00+00:00",
  "data": {
    "threadId": "thread_xyz",
    "messageId": "msg_abc123"
  },
  "meta": {}
}
```

#### `gmail.label_added` / `gmail.label_removed`

```json
{
  "id": 12,
  "event_id": "msg_abc123-5001-lab-add",
  "event_type": "gmail.label_added",
  "entity_id": "msg_abc123",
  "created_at": "2024-03-15T10:10:00+00:00",
  "data": {
    "threadId": "thread_xyz",
    "messageId": "msg_abc123",
    "labelIds": ["STARRED"],
    "allLabelIds": ["INBOX", "STARRED"]
  },
  "meta": {}
}
```

- `labelIds`: The labels that were just added or removed.
- `allLabelIds`: The full set of labels on the message after the change.
