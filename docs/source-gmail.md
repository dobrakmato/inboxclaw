# Gmail Source

The Gmail source allows the ingest pipeline to fetch recent emails from a Google account and convert them into events.

## Why use this source?

This source is ideal for building workflows that respond to incoming emails, such as:
- Automatic ticket creation from customer emails.
- Archiving important communications.
- Triggering notifications based on specific senders or subjects.

**Pros:**
- Simple setup using Google OAuth.
- Efficiently fetches only the most recent emails.
- Preserves key metadata (sender, recipient, subject, snippet).

**Cons:**
- Currently fetches only the 50 most recent emails per poll (basic implementation).
- Requires a persistent OAuth token.

## Core Concepts

### Polling
The source periodically "polls" the Gmail API to check for new messages. You can configure how often this happens using a human-readable interval.

### Deduplication
Each email has a unique `msg_id` from Google. The source uses this ID to ensure that each email is only published as an event once, even if it's found multiple times during polling.

## Configuration

### Minimal Configuration

To get started, you only need to provide the `token_file` (generated via the `google auth` CLI) and the polling interval.

```yaml
sources:
  my_inbox:
    type: gmail
    token_file: "data/gmail_token.json"
    poll_interval: "5m"
```

### Full Configuration

You can also specify a custom name and different intervals.

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

When an email is fetched, it creates an event with the type `gmail.email`. The `data` field contains:

```json
{
  "threadId": "...",
  "snippet": "Brief preview of the email content...",
  "from": "Sender Name <sender@example.com>",
  "to": "Recipient <recipient@example.com>",
  "subject": "Email Subject",
  "date": "Date header from email",
  "internalDate": "1234567890"
}
```

The `occurred_at` field of the event is automatically set based on the `internalDate` (the time the message was received).
