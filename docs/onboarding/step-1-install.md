# Step 1: Install and Run Inboxclaw

This step gets Inboxclaw running on your machine with a minimal test configuration so you can see it in action before connecting real accounts.

## Prerequisites

- **Python 3.11+** installed on your machine.
- **Git** to clone the repository.

## Clone the Repository

```bash
git clone https://github.com/dobrakmato/inboxclaw.git
cd inboxclaw
```

## Create a Minimal Configuration

Copy the example configuration file to get started:

```bash
cp config.example.yaml config.yaml
```

Or create a minimal `config.yaml` by hand. This configuration uses a mock source (fake test data) and an SSE sink (a simple live stream you can watch in your browser):

```yaml
$schema: ./config.schema.json
server:
  host: 127.0.0.1
  port: 8001

database:
  retention_days: 7
  db_path: ./data/data.db

sources:
  mock: {}

sink:
  sse:
    match: "*"
```

::: tip What's a mock source?
It generates fake events so you can test that everything works without connecting any real accounts yet. You'll replace it with real sources in [Step 2](step-2-sources.md).
:::

## Start Inboxclaw

```bash
python main.py listen
```

On the first run, this automatically creates a virtual environment, installs all dependencies, and sets up the `inboxclaw` command. You don't need to set anything up manually.

::: tip The `inboxclaw` command
After the first run completes, the `inboxclaw` CLI command becomes available. From this point on, you can use `inboxclaw` instead of `python main.py` for all commands. The rest of this tutorial uses `inboxclaw`.
:::

Once it's running, open the API docs in your browser at **http://127.0.0.1:8001/docs** to see the available endpoints.

## Quick Introduction to the CLI

The `inboxclaw` command is your main tool for interacting with Inboxclaw. Here are the commands you'll use most:

| Command | What it does |
|:--|:--|
| `inboxclaw listen` | Starts the Inboxclaw server (the main command). |
| `inboxclaw status` | Shows the current status — is it running? Any errors? |
| `inboxclaw events` | Lists the latest events that have been processed. |
| `inboxclaw restart` | Validates your config and restarts the service. |
| `inboxclaw update` | Checks for updates and installs them. |

For the full list of commands, see the [CLI Reference](../cli.md).

## Verify It Works

After starting with `inboxclaw listen`, you should see log output showing that the mock source is generating events. Run this in another terminal:

```bash
inboxclaw events
```

You should see a list of test events. If you do — congratulations, Inboxclaw is working!

Alternatively, you can try interactively subscribing to the SSE stream:
```bash
inboxclaw subscribe
```

## Next Step

→ [Step 2: Configure Sources](step-2-sources.md) — connect your real accounts (Gmail, bank, calendar, etc.).
