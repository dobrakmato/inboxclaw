# Google Authentication CLI

Connecting your ingest pipeline to Google services (Gmail, Calendar, Drive, Docs) requires a secure connection using OAuth 2.0. This guide explains how to use the built-in CLI tool to authorize access and generate a token that the pipeline can use.

## Why use this?

Before the ingest pipeline can read your emails or calendar events, you must grant it permission. This tool handles the "handshake" between your computer and Google's servers.

- **Security**: Your password is never shared with this application.
- **Control**: You can revoke access at any time through your Google Account settings.
- **Convenience**: Once authorized, the pipeline can run in the background without asking for permission again until the token expires or is revoked.

## Before You Start

You will need Google Cloud credentials (a Client ID and Client Secret). You can get these by creating a "Desktop App" in the [Google Cloud Console](https://console.cloud.google.com/).

## How to Authorize

The authorization is a two-step process.

### Step 1: Generate the Authorization Link

Run the following command in your terminal. Replace the placeholders with your actual Client ID and Secret, and specify where you want to save the token.

You must also provide a list of **scope aliases** (see the table below) for the services you want to access.

```bash
python main.py google auth --client-id "YOUR_CLIENT_ID" --client-secret "YOUR_CLIENT_SECRET" --token "data/google_token.json" --scopes "gmail,calendar,drive"
```

The tool will print a long URL.

### Scope Aliases

Use these short names with the `--scopes` parameter to grant specific permissions:

| Alias      | Permission Granted                    | Use Case                                        |
|:-----------|:--------------------------------------|:------------------------------------------------|
| `gmail`    | Read-only access to emails            | Fetching recent emails from your inbox.         |
| `calendar` | Read-only access to calendar events   | Monitoring your schedule for new events.        |
| `drive`    | Read-only access to file metadata     | Tracking changes to files in your Google Drive. |
| `docs`     | Read-only access to document metadata | Monitoring modifications to Google Docs.        |
| `contacts` | Read-only access to contacts          | Accessing your contact list information.        |

> **Note**: You can combine multiple aliases using commas (e.g., `--scopes "gmail,calendar"`).

### Step 2: Approve Access and Paste the Result

1.  **Copy the URL** printed in Step 1 and paste it into your web browser.
2.  **Sign in** with your Google account and review the permissions requested.
3.  **Approve** the request.
4.  After approving, your browser will try to redirect you to a page that might not load (e.g., `http://localhost:8765/...`). This is normal!
5.  **Copy the entire URL** from your browser's address bar.
6.  Go back to your terminal and **paste the URL** when prompted.

The tool will now save a `google_token.json` file in your `data/` folder.

## Configuration

Once you have the token file, update your `config.yaml` to point to it:

```yaml
sources:
  gmail:
    token_file: "data/google_token.json"
    poll_interval: "5m"
```

## Tips

- **Scopes**: Use the scope aliases listed above (e.g., `gmail`, `drive`, `calendar`) in the `--scopes` parameter, separated by commas.
- **Credentials File**: If you have a `credentials.json` file downloaded from Google Cloud, you can use `--credentials-file path/to/credentials.json` instead of manually typing the ID and Secret.
