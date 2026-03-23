# Command Line Interface (CLI)

Inboxclaw provides a command-line interface for running the server and performing maintenance or setup tasks, such as authenticating with external APIs.

The CLI is built with `Click` and is invoked using the `inboxclaw` command.

::: tip When is the `inboxclaw` command available?
The `inboxclaw` command becomes available automatically after the first run of `python main.py` (which sets up the virtual environment and installs dependencies). After that, you can use `inboxclaw` for all commands. If you haven't run the initial setup yet, see [Step 1: Install](onboarding/step-1-install.md).
:::

## Core Commands

### `status`
Checks the status of the Inboxclaw system, including the systemd service, logs, healthcheck endpoint, version info, and database statistics.

**Usage:**
```bash
inboxclaw status [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).
- `--service-name TEXT`: Name of the systemd service to check (default: `inboxclaw`).

**Example:**
```bash
inboxclaw status
```

### `events`
Displays the latest published events from the database.

**Usage:**
```bash
inboxclaw events [OPTIONS]
```

**Options:**
- `-n INTEGER`: Number of latest published events to display (default: 10).
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).

**Example:**
```bash
# Show the last 20 published events
inboxclaw events -n 20
```

### `pending-events`
Displays the latest pending (coalescing) events from the database.

**Usage:**
```bash
inboxclaw pending-events [OPTIONS]
```

**Options:**
- `-n INTEGER`: Number of latest pending events to display (default: 10).
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).

**Example:**
```bash
# Show the last 5 pending events
inboxclaw pending-events -n 5
```

### `logs`
Displays the logs for the Inboxclaw service using `journalctl`. This is only available on Linux systems where Inboxclaw is installed as a systemd service.

**Usage:**
```bash
inboxclaw logs [OPTIONS]
```

**Options:**
- `-n, --lines INTEGER`: Number of log lines to show (default: 20).
- `-f, --follow`: Follow the logs in real-time.
- `--service-name TEXT`: Name of the systemd service (default: `inboxclaw`).

**Example:**
```bash
# Show last 50 lines and follow
inboxclaw logs -n 50 -f
```

### `restart`
Restarts the Inboxclaw `systemd` service. This command **validates the configuration file** before triggering the restart to ensure the service starts correctly.

On non-Linux systems, this command will only perform configuration validation and skip the service restart.

**Usage:**
```bash
inboxclaw restart [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file to validate (default: `config.yaml`).
- `--service-name TEXT`: Name of the systemd service to restart (default: `inboxclaw`).
- `--user`: Restart as a user service (default). Does not require root.
- `--system`: Restart as a system-wide service (requires root).

**Example:**
```bash
inboxclaw restart
```

### `update`
Checks the GitHub repository for updates, pulls them, and installs any new dependencies.

**Usage:**
```bash
inboxclaw update [OPTIONS]
```

**Options:**
- `--force`: Force update even if no changes are detected.

**Example:**
```bash
inboxclaw update
```

### `listen`
Starts the Inboxclaw server. This command is the main entry point for running the application.

**Usage:**
```bash
inboxclaw listen [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).

**Example:**
```bash
inboxclaw listen --config my-custom-config.yaml
```

### `subscribe`
Subscribes to an SSE (Server-Sent Events) endpoint and dumps raw JSON payloads to `stdout`. This is useful for debugging or integrating with other tools that can consume JSON from a pipe.

**Usage:**
```bash
inboxclaw subscribe [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).
- `--sink TEXT`: Specific SSE sink name to use (if multiple SSE sinks are configured).

**Example:**
```bash
inboxclaw subscribe
```

### `pull`
Runs a pull request against a locally configured HTTP Pull sink. It extracts a batch of events and outputs the raw JSON response to `stdout`, making it suitable for CLI integrations.

**Usage:**
```bash
inboxclaw pull [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).
- `--name TEXT`: Name of the HTTP Pull sink to use (if multiple are configured).
- `--event-type TEXT`: Filter by event type (supports `*` and `.*`).
- `--batch-size INTEGER`: Limit the number of events to extract (must be >= 1).

**Example:**
```bash
# Pull a batch of events
inboxclaw pull --event-type "gmail.*" --batch-size 5

# Output:
# {"batch_id": "abc-123", "events": [...], "remaining_events": 0}
```

### `pull-mark-processed`
Marks a specific batch of events as processed in a locally configured HTTP Pull sink. This should be called after you have successfully processed the events returned by the `pull` command.

**Usage:**
```bash
inboxclaw pull-mark-processed [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).
- `--name TEXT`: Name of the HTTP Pull sink to use (if multiple are configured).
- `--batch-id TEXT`: **Required**. The batch ID returned by the `pull` command.

**Example:**
```bash
inboxclaw pull-mark-processed --batch-id "abc-123"
```

### `install`
Installs Inboxclaw as a `systemd` service on Linux. This allows the pipeline to start automatically on boot and restart if it crashes.

Additionally, it creates a symlink to the `inboxclaw` CLI command in your system PATH (`~/.local/bin` for user installations or `/usr/local/bin` for system-wide ones), making `inboxclaw` accessible from any directory outside the virtual environment.

**Usage:**
```bash
inboxclaw install [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).
- `--user`: Install as a user service (default). Does not require root.
- `--system`: Install as a system-wide service (requires root).
- `--name TEXT`: Name of the systemd service (default: `inboxclaw`).

**Examples:**

**User Installation (Non-root):**
```bash
inboxclaw install --user
```
After installing as a user, you may want to enable "linger" for your user so the service starts on boot without you having to log in:
```bash
loginctl enable-linger $USER
```

**System-wide Installation (Root):**
```bash
sudo inboxclaw install --system
```

---

## Service-Specific Commands

### `google`
Commands for interacting with Google APIs (Gmail, Drive, Calendar).

#### `google auth`
Starts the OAuth 2.0 flow to authorize the pipeline to access your Google account. This command will open a browser for you to sign in and will save the credentials to the configured token file.

See [Google Auth Guide](google-auth-cli.md) for more details.

#### `google list-calendars`
Lists all available calendars in your Google account. This is useful for finding the `calendar_id` needed for the [Google Calendar source](source-google-calendar.md).

---

### `nordigen`
Commands for GoCardless (formerly Nordigen) Bank Account Data API.

#### `nordigen auth`
Exchanges your GoCardless API credentials (`secret_id` and `secret_key`) for a long-lived access token.

#### `nordigen connect`
Starts the process of connecting a bank account. It generates a requisition link that you must open in your browser to authorize access to your bank. Once authorized, it will display the account IDs you can add to your `config.yaml`.

See [Nordigen Source](source-nordigen.md) for more information.

---

## Global Options

- `--help`: Show the help message for any command or sub-command.

**Example:**
```bash
inboxclaw google auth --help
```
