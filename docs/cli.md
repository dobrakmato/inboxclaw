# Command Line Interface (CLI)

Inboxclaw provides a command-line interface for running the server and performing maintenance or setup tasks, such as authenticating with external APIs.

The CLI is built with `Click` and can be invoked using `python main.py`.

## Core Commands

### `status`
Checks the status of the Inboxclaw system, including the systemd service, logs, healthcheck endpoint, version info, and database statistics.

**Usage:**
```bash
python main.py status [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).
- `--service-name TEXT`: Name of the systemd service to check (default: `inboxclaw`).

**Example:**
```bash
python main.py status
```

### `restart`
Restarts the Inboxclaw `systemd` service. This is useful for applying configuration changes or restarting the service if it becomes unresponsive.

**Usage:**
```bash
python main.py restart [OPTIONS]
```

**Options:**
- `--service-name TEXT`: Name of the systemd service to restart (default: `inboxclaw`).
- `--user`: Restart as a user service (default). Does not require root.
- `--system`: Restart as a system-wide service (requires root).

**Example:**
```bash
python main.py restart
```

### `update`
Checks the GitHub repository for updates, pulls them, and installs any new dependencies.

**Usage:**
```bash
python main.py update [OPTIONS]
```

**Options:**
- `--force`: Force update even if no changes are detected.

**Example:**
```bash
python main.py update
```

### `listen`
Starts the Inboxclaw server. This command is the main entry point for running the application.

**Usage:**
```bash
python main.py listen [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).

**Example:**
```bash
python main.py listen --config my-custom-config.yaml
```

### `install`
Installs Inboxclaw as a `systemd` service on Linux. This allows the pipeline to start automatically on boot and restart if it crashes.

Additionally, it creates a symlink to the `inboxclaw` CLI command in your PATH (`~/.local/bin` for user installations or `/usr/local/bin` for system-wide ones), allowing you to run `inboxclaw` from any directory.

**Usage:**
```bash
python main.py install [OPTIONS]
```

**Options:**
- `--config TEXT`: Path to the configuration file (default: `config.yaml`).
- `--user`: Install as a user service (default). Does not require root.
- `--system`: Install as a system-wide service (requires root).
- `--name TEXT`: Name of the systemd service (default: `inboxclaw`).

**Examples:**

**User Installation (Non-root):**
```bash
python main.py install --user
```
After installing as a user, you may want to enable "linger" for your user so the service starts on boot without you having to log in:
```bash
loginctl enable-linger $USER
```

**System-wide Installation (Root):**
```bash
sudo python main.py install --system
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
python main.py google auth --help
```
