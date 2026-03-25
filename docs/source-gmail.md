# Gmail Source

The Gmail source monitors a Google mailbox and emits events when emails are received, sent, or deleted. It can also optionally emit events when labels change (off by default). It uses the Gmail History API for efficient incremental sync — only new changes are fetched on each poll.

Use this source to build workflows that react to incoming emails: ticket creation, archiving, notifications based on sender or subject, etc.

## Getting Started

### 1. Authorize access

Generate a Google OAuth token with the `gmail` scope using the [Google Auth CLI](google-auth-cli.md):

```bash
inboxclaw google auth \
  --credentials-file data/credentials.json \
  --scopes gmail \
  --token data/google_token.json
```

### 2. Add the source to `config.yaml`

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
```

The source will start polling on startup. On the first run it initializes a `historyId` cursor using the `users.getProfile` API and begins tracking changes from that point forward.

## Core Concepts

### Incremental Sync

The source uses Gmail's `history.list` API with a stored `historyId` cursor. Each poll fetches only the changes since the last cursor, making it efficient even for busy mailboxes. If the cursor expires (too old), the source re-initializes automatically.

### Label Filtering

By default, messages with the `SPAM` label are skipped. You can customize this with `exclude_label_ids` — any message that has at least one matching label is ignored.

### Content Filtering

You can filter emails by their subject, snippet (body preview), or sender using either exact string matching (case-insensitive) or regular expressions. This is useful for ignoring automated emails, newsletters, or specific alerts.

Each filter is a named object within a list. You specify which field to check with `in` (`subject`, `snippet`, or `sender`) and the match criteria with `contains` or `regex`.

If an email matches **any** of the configured filters, it is ignored and no event is emitted.

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
    filters:
      - ignore_internal:
          in: subject
          contains: "[Internal]"
      - no_alerts:
          in: subject
          regex: "^Alert:.*(Resolved|Fixed)$"
      - unsubscribe_newsletters:
          in: snippet
          contains: "Unsubscribe"
      - ignore_sender:
          in: sender
          contains: "no-reply@important-service.com"
```

## Configuration

### Minimal Configuration

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
```

Defaults: `poll_interval: "10m"`, `exclude_label_ids: ["SPAM"]`, `emit_label_events: false`.

### Full Configuration

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
    poll_interval: "1m"
    exclude_label_ids: ["SPAM", "TRASH", "CATEGORY_PROMOTIONS"]
    emit_label_events: true
```

### Configuration Reference

| Parameter           | Type     | Default      | Description                                                                                  |
|:--------------------|:---------|:-------------|:---------------------------------------------------------------------------------------------|
| `token_file`        | `string` | Required     | Path to the Google OAuth2 token file (created via [Google Auth CLI](google-auth-cli.md)).     |
| `poll_interval`     | `string` | `"10m"`      | How often to check for changes. Supports human-readable intervals (e.g. `"5m"`, `"30s"`).    |
| `exclude_label_ids` | `list`   | `["SPAM"]`   | Messages with any of these labels are skipped. Common labels: `SPAM`, `TRASH`, `UNREAD`, `STARRED`, `IMPORTANT`, `INBOX`, `CATEGORY_PERSONAL`, `CATEGORY_SOCIAL`, `CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, `CATEGORY_FORUMS`. |
| `emit_label_events` | `boolean`| `false`      | Whether to emit events when labels are added or removed from emails.                         |
| `filters`           | `list`   | `[]`         | Content-based filters to skip emails. Each item is a filter object.                          |

### Filter Object

The `filters` list contains objects where the key is the name of the filter (for logging purposes) and the value is a filter definition:

| Property   | Type     | Required | Description                                                                 |
|:-----------|:---------|:---------|:----------------------------------------------------------------------------|
| `in`       | `string` | Yes      | Which field to search in. Must be `subject`, `snippet`, or `sender`.        |
| `contains` | `string` | No       | Filters if the field contains this string (case-insensitive).               |
| `regex`    | `string` | No       | Filters if the field matches this regular expression.                       |

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
    "from": {
      "name": "Sender Name",
      "email": "sender@example.com"
    },
    "to": {
      "name": "Recipient",
      "email": "recipient@example.com"
    },
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
