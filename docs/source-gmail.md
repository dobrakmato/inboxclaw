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

You can also specify a custom name and different intervals. Note that `poll_interval` is optional and defaults to `10m`.

```yaml
sources:
  customer_support:
    type: gmail
    token_file: "tokens/support_token.json"
    poll_interval: "1m"
```

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
| **Type** | `gmail.email` | Triggered when a new email is fetched. |
| **Entity ID** | Google Message ID | Uniquely identifies the email message. |

### Data Payload

When an email is fetched, it creates an event with the type `gmail.email`. The `data` field contains:

```json
{
  "threadId": "...",
  "snippet": "Brief preview of the email content...",
  "from": "Sender Name <sender@example.com>",
  "to": "Recipient <recipient@example.com>",
  "subject": "Email Subject",
  "date": "Date header from email",
  "internalDate": "1234567890",
  "labelIds": ["INBOX", "UNREAD"]
}
```

The `occurred_at` field of the event is automatically set based on the `internalDate` (the time the message was received).
