# Step 2: Configure Sources

Now that Inboxclaw is running, it's time to connect it to your real accounts. Sources are where your data comes from — Gmail, your bank, Google Calendar, and so on.

## What Is a Source?

A source is a connector that watches an external service for changes. Each source knows how to talk to one specific service, check for new activity, and turn it into a standardized event that the rest of Inboxclaw can work with.

You can run as many sources as you want at the same time. Each one polls its service independently.

## How the Configuration File Works

All sources are defined in your `config.yaml` under the `sources` section. Each source gets a name (you choose it) and a set of options specific to that service.

Here's what it looks like with two sources:

```yaml
sources:
  my_email:
    type: gmail
    poll_interval: "5m"
    credentials_path: ./credentials.json

  my_bank:
    type: fio
    poll_interval: "1h"
    token: "${FIO_API_TOKEN}"
```

A few things to notice:

- **`type`** tells Inboxclaw which connector to use.
- **`poll_interval`** controls how often Inboxclaw checks for new data. You can use human-readable values like `"30s"`, `"5m"`, or `"1h"`.
- **Secrets** like API tokens can be stored in environment variables and referenced with `${VAR_NAME}`. Create a `.env` file in the project root to keep them out of your config.

## Google Services: Authentication

If you're connecting Gmail, Google Calendar, or Google Drive, a simple API key isn't enough. Google requires a one-time authentication step through your browser:

```bash
inboxclaw google auth
```

This opens a browser window where you sign in with your Google account and grant Inboxclaw permission to read your data. The credentials are saved locally so you only need to do this once.

For the full details, see the [Google Auth CLI Guide](../google-auth-cli.md).

## What Is Coalescing?

Some sources are noisy. For example, if someone edits a Google Doc ten times in five minutes, you probably don't want ten separate events — you want one event that says "this document was updated."

That's what coalescing does. It groups rapid-fire updates for the same item and waits for things to settle down before emitting a single, clean event. This keeps your sinks from being flooded with duplicate notifications.

Coalescing is configured per source. For the full explanation, see [Event Coalescing](../coalescing.md).

## Available Sources

| Source | What it watches | Docs |
|:--|:--|:--|
| Gmail | New emails and label changes | [Gmail Source](../source-gmail.md) |
| Google Calendar | New and updated calendar events | [Google Calendar Source](../source-google-calendar.md) |
| Google Drive | File changes and new files | [Google Drive Source](../source-google-drive.md) |
| Fio Banka | Bank transactions | [Fio Source](../source-fio.md) |
| Faktury Online | Invoice changes | [Faktury Online Source](../source-faktury-online.md) |
| Home Assistant | Device and sensor state changes | [Home Assistant Source](../source-home-assistant.md) |
| GoCardless / Nordigen | Bank account data across EU banks | [Nordigen Source](../source-nordigen.md) |
| Mock | Fake test events | [Mock Source](../source-mock.md) |

Click through to the source you want to set up — each page has its own getting started section with the exact configuration you need.

For a general overview of how all sources work, see [Sources Overview](../sources-general.md).

## Next Step

→ [Step 3: Configure Sinks](step-3-sinks.md) — decide where your events should be delivered.
