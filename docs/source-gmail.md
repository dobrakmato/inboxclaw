# Gmail Source

The Gmail source allows the ingest pipeline to fetch recent emails from a Google account and convert them into events.

## Why use this source?

This source is ideal for building workflows that respond to incoming emails, such as:
- Automatic ticket creation from customer emails.
- Archiving important communications.
- Triggering notifications based on specific senders or subjects.

## Core Concepts

### Polling
The source periodically "polls" the Gmail API to check for new messages. You can configure how often this happens using a human-readable interval.

### Incremental Sync (History API)
Unlike simple listing, this source uses the Gmail `history.list` API. It maintains a `historyId` cursor in the database. On each poll, it only asks for changes that happened *after* that ID. This is much more efficient and reliable for busy mailboxes.

### Deduplication
Each email has a unique `msg_id` from Google. The source uses this ID as the `event_id` and `entity_id` to ensure that each email is only published as an event once.

## Configuration

### Minimal Configuration

To get started, you only need to provide the `token_file` (generated via the `google auth` CLI) and the polling interval.

```yaml
sources:
  my_inbox:
    type: gmail
    token_file: "data/gmail_token.json"
    poll_interval: "10m"
```

### Full Configuration

You can customize the polling interval and provide a list of label IDs to exclude. By default, `SPAM` messages are excluded.

```yaml
sources:
  customer_support:
    type: gmail
    token_file: "tokens/support_token.json"
    poll_interval: "1m"
    exclude_label_ids: ["SPAM", "TRASH", "CATEGORY_PROMOTIONS"]
```

### Filtering by Label IDs

The `exclude_label_ids` parameter allows you to skip emails that have specific Gmail labels. 

- **Default behavior**: If not specified, `exclude_label_ids` defaults to `["SPAM"]`.
- **How it works**: If an email has *at least one* label that matches any ID in the `exclude_label_ids` list, the source will skip it.
- **Common Labels**: `SPAM`, `TRASH`, `UNREAD`, `STARRED`, `IMPORTANT`, `INBOX`, `CATEGORY_PERSONAL`, `CATEGORY_SOCIAL`, `CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, `CATEGORY_FORUMS`.

## OAuth Scopes Required

To use this source, you must authorize the application with at least the following scope:
- `gmail` (alias for `https://www.googleapis.com/auth/gmail.readonly`)

Use the CLI to generate the token:
```bash
python main.py google auth --scopes gmail --token data/gmail_token.json
```

## Event Data structure

| Parameter | Value | Description |
| :--- | :--- | :--- |
| **Type** | `gmail.message_added` | Triggered when a new email is received. |
| **Type** | `gmail.message_deleted` | Triggered when an email is deleted. |
| **Type** | `gmail.label_added` | Triggered when a label is added to an email. |
| **Type** | `gmail.label_removed` | Triggered when a label is removed from an email. |
| **Entity ID** | Google Message ID | Uniquely identifies the email message. |

### Data Payloads

#### gmail.message_added

The `data` field contains:

```json
{
  "threadId": "...",
  "messageId": "...",
  "snippet": "Brief preview of the email content...",
  "from": "Sender Name <sender@example.com>",
  "to": "Recipient <recipient@example.com>",
  "subject": "Email Subject",
  "date": "Date header from email",
  "labelIds": ["INBOX", "UNREAD"]
}
```

The `occurred_at` field is set based on the time the message was received.

#### gmail.message_deleted

The `data` field contains:

```json
{
  "threadId": "...",
  "messageId": "...",
  "historyId": "..."
}
```

#### gmail.label_added / gmail.label_removed

The `data` field contains:

```json
{
  "threadId": "...",
  "messageId": "...",
  "historyId": "...",
  "labelIds": ["LABEL_ID"],
  "allLabelIds": ["INBOX", "LABEL_ID"]
}
```

`labelIds` contains the IDs of the labels that were just added or removed. `allLabelIds` contains the full set of labels on the message after the change.
