# Google Authentication CLI

Before the pipeline can access Google services (Gmail, Calendar, Drive), you need to grant it permission using OAuth 2.0. This CLI tool handles the authorization flow and saves a token file that the pipeline uses to authenticate.

Your password is never shared with the pipeline. You can revoke access at any time through your [Google Account settings](https://myaccount.google.com/permissions). Once authorized, the pipeline runs in the background without asking for permission again until the token expires or is revoked.

## Prerequisites

You need Google Cloud credentials. Create a "Desktop App" in the [Google Cloud Console](https://console.cloud.google.com/) and download the `credentials.json` file.

The CLI tool requires this file via the `--credentials-file` parameter.

## How to Authorize

### Step 1: Run the auth command

```bash
inboxclaw google auth \
  --credentials-file data/credentials.json \
  --token "data/google_token.json" \
  --scopes "gmail,calendar,drive"
```

The tool will print an authorization URL.

### Step 2: Approve access in your browser

1. Copy the URL and open it in your browser.
2. Sign in with your Google account and review the permissions.
3. Approve the request.
4. Your browser will redirect to a URL that may not load (e.g. `http://localhost:8765/...`). This is normal.
5. Copy the **entire URL** from your browser's address bar.
6. Paste it back into the terminal when prompted.

The tool saves a `google_token.json` file at the path you specified.

### Step 3: Use the token in your config

```yaml
sources:
  my_gmail:
    type: gmail
    token_file: "data/google_token.json"
    poll_interval: "5m"
```

Multiple sources can share the same token file, as long as the token was created with all the required scopes.

## Scope Aliases

Use these short names with the `--scopes` parameter (comma-separated):

| Alias            | Permission Granted                 | Used by                                                                    |
|:-----------------|:-----------------------------------|:---------------------------------------------------------------------------|
| `gmail`          | Read-only access to emails         | [Gmail source](source-gmail.md)                                           |
| `calendar`       | Read-only access to calendar       | [Google Calendar source](source-google-calendar.md)                        |
| `drive`          | Read-only access to files          | [Google Drive source](source-google-drive.md) (metadata + content diffs)   |
| `drive_metadata` | Read-only access to file metadata  | [Google Drive source](source-google-drive.md) (metadata only, no diffs)    |
| `docs`           | Read-only access to files          | Google Docs content access                                                 |
| `contacts`       | Read-only access to contacts       | Contact list access                                                        |
| `all`            | Multiple permissions               | Grants `gmail`, `drive`, `calendar`, and `contacts` together               |
