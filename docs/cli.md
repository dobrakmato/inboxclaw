# Command Line Interface (CLI)

The Ingest Pipeline provides a command-line interface for running the server and performing maintenance or setup tasks, such as authenticating with external APIs.

The CLI is built with `Click` and can be invoked using `python main.py`.

## Core Commands

### `listen`
Starts the Ingest Pipeline server. This command is the main entry point for running the application.

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
