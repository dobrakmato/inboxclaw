# GoCardless Bank Account Data (Nordigen) Source

The GoCardless Bank Account Data source (formerly known as Nordigen) connects your bank accounts to the pipeline and emits an event for every transaction — booked or pending. It works with thousands of banks across Europe and beyond, using the open-banking consent flow to access your data without ever storing your banking credentials.

Use this source to automate financial workflows: track incoming payments, monitor spending, reconcile accounts, or get instant notifications for any account movement.

## Core Concepts

### One source per bank account

Each source entry in `config.yaml` monitors exactly one bank account. If you have two bank accounts, you add two source entries. This keeps configuration explicit and makes it easy to apply different settings (poll interval, label) per account.

### How bank access works

GoCardless acts as a regulated intermediary. You grant consent through your bank's own login page; GoCardless never sees your password. After consent, GoCardless fetches transactions on your behalf and exposes them via its API. Your banking credentials are never stored anywhere in this pipeline.

### Date-window polling, not cursors

The GoCardless API does not provide a cursor-based incremental feed. Instead, the source polls a date window on each cycle:

```
date_from = last_booked_date - 3 days   (overlap window)
date_to   = today
```

The 3-day overlap is intentional: banks sometimes retroactively enrich or correct recent transactions (e.g., a pending item settles with a different description). The pipeline deduplicates by canonical transaction ID, so re-fetching the overlap never produces duplicate events.

### Stable transaction IDs

Not all banks provide stable transaction identifiers. The source uses this priority order to compute a canonical ID:

1. `internalTransactionId` — GoCardless internal ID (most stable)
2. `transactionId` — bank-provided ID
3. `entryReference` — financial institution reference
4. Fingerprint hash — SHA-256 of `(account_id, status, date, amount, currency, counterpart, remittance, bank_code)` when none of the above are present

### Booked vs. pending

The API returns two separate arrays per poll: `booked` (settled) and `pending` (provisional). The source emits events for both. Pending transactions carry a `.pending` suffix in their event type and may disappear or change before they settle.

### Rate limits and poll scheduling

GoCardless documents a lower bound of 4 API calls per day per endpoint per account. The minimum poll interval is therefore enforced at **6 hours** — even if you configure a shorter value, the source will use 6 hours. Poll schedule state (`last_poll_at`, `next_poll_at`, `backoff_until`) is stored in the per-source KV cache so the schedule survives restarts.

If the source hits a rate limit (HTTP 429), it backs off for 6 hours before retrying. Institution errors (HTTP 500/503) trigger a 1-hour backoff.

## Getting Started

### Step 1 — Create a GoCardless account and get API credentials

1. Sign up at [bankaccountdata.gocardless.com](https://bankaccountdata.gocardless.com/).
2. Go to **User Secrets** in the dashboard.
3. Create a new secret. Copy the `secret_id` and `secret_key`.
4. Add them to your `.env` file:

```env
NORDIGEN_SECRET_ID=your_secret_id
NORDIGEN_SECRET_KEY=your_secret_key
```

### Step 2 — Mint a refresh token

The refresh token is a long-lived credential (~30 days) that the pipeline uses to obtain short-lived access tokens automatically. Run this once:

```
python main.py nordigen auth
```

The command contacts GoCardless, prints your refresh token, and tells you exactly what to add to your `.env`:

```env
NORDIGEN_REFRESH_TOKEN=your_refresh_token
```

> **Keep this token secret.** It grants read access to any bank account you connect.

### Step 3 — Connect a bank account

Run the interactive connect wizard:

```
python main.py nordigen connect --country CZ
```

Replace `CZ` with the [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2) code for your bank's country (e.g. `DE`, `GB`, `SK`).

The wizard will:
1. Authenticate with GoCardless using your credentials.
2. Show a searchable list of available banks — type part of the name to filter.
3. Open a browser link to your bank's consent page.
4. Wait for you to complete authentication and press Enter.
5. Write each linked account as a **separate source entry** in your `config.yaml`.

To connect a second bank account, simply run the command again. Each run adds a new source entry without touching existing ones.

### Step 4 — Restart the pipeline

```
python main.py listen
```

The source will perform an initial sync covering `initial_history_days` of history (default: 90 days), then poll every `poll_interval` (default: 6 hours).

## Configuration

### Minimal Configuration

After running the connect wizard, your `config.yaml` will contain one entry per account:

```yaml
sources:
  nordigen_3fa85f64:
    type: nordigen
    account_id: "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    label: "Main Checking"
```

The `secret_id`, `secret_key`, and `refresh_token` are read from environment variables automatically.

### Multiple accounts

Each account is its own source entry:

```yaml
sources:
  nordigen_checking:
    type: nordigen
    account_id: "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    label: "Checking"

  nordigen_savings:
    type: nordigen
    account_id: "7cb12a34-1234-5678-abcd-ef0123456789"
    label: "Savings"
    poll_interval: "12h"
```

### Full Runtime Configuration

The `secret_id`, `secret_key`, and `refresh_token` are typically read from environment variables, but they can be overridden in the configuration if necessary.

```yaml
sources:
  nordigen_checking:
    type: nordigen
    account_id: "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    label: "Main Checking"
    poll_interval: "6h"
    initial_history_days: 90
```

### Configuration Reference

| Parameter              | Type     | Default | Description                                                                                     |
|:-----------------------|:---------|:--------|:------------------------------------------------------------------------------------------------|
| `account_id`           | `string` | `""`    | GoCardless account UUID, obtained via the `nordigen connect` wizard.                            |
| `label`                | `string` | `null`  | Human-readable name shown in event data (e.g. `"Checking"`, `"Savings"`).                      |
| `poll_interval`        | `string` | `"6h"`  | How often to check for new transactions. Minimum enforced at 6h regardless of configured value. |
| `initial_history_days` | `int`    | `90`    | How many days of history to fetch on the very first sync. Capped by your end-user agreement.    |

### Onboarding & Authentication (Implicit)

These parameters are primarily used for authentication and are typically provided via environment variables. While they can be placed in `config.yaml`, it is recommended to keep them in `.env`.

| Parameter       | Type     | Default | Description                                                                          |
|:----------------|:---------|:--------|:-------------------------------------------------------------------------------------|
| `secret_id`     | `string` | Env var | GoCardless `secret_id`. Defaults to `NORDIGEN_SECRET_ID` environment variable.       |
| `secret_key`    | `string` | Env var | GoCardless `secret_key`. Defaults to `NORDIGEN_SECRET_KEY` environment variable.     |
| `refresh_token` | `string` | Env var | Long-lived refresh token. Defaults to `NORDIGEN_REFRESH_TOKEN` environment variable. |

## Event Definitions

| Type                                  | Entity ID | Description                                              |
|:--------------------------------------|:----------|:---------------------------------------------------------|
| `nordigen.transaction.credit`         | *(none)*  | Settled incoming transaction (amount > 0).               |
| `nordigen.transaction.debit`          | *(none)*  | Settled outgoing transaction (amount < 0).               |
| `nordigen.transaction`                | *(none)*  | Settled transaction with amount exactly 0 (rare).        |
| `nordigen.transaction.credit.pending` | *(none)*  | Pending incoming transaction — may change or disappear.  |
| `nordigen.transaction.debit.pending`  | *(none)*  | Pending outgoing transaction — may change or disappear.  |
| `nordigen.transaction.pending`        | *(none)*  | Pending transaction with amount exactly 0 (rare).        |
| `nordigen.error.access_expired`       | *(none)*  | Access token expired or revoked — action required.       |
| `nordigen.error.access_forbidden`     | *(none)*  | Account access forbidden — action required.              |

Transactions do not have an `entity_id` because they are not tied to a persistent entity in the pipeline's data model.

### Transaction Event Example

```json
{
  "id": 42,
  "event_id": "nordigen_3fa85f64-5717-4562-b3fc-2c963f66afa6_INT-20240315-001",
  "event_type": "nordigen.transaction.credit",
  "entity_id": null,
  "created_at": "2024-03-15T00:00:00+00:00",
  "data": {
    "internalTransactionId": "INT-20240315-001",
    "transactionAmount": {"amount": "1250.00", "currency": "EUR"},
    "bookingDate": "2024-03-15",
    "creditorName": "ACME Corp",
    "remittanceInformationUnstructured": "Invoice INV-2024-042",
    "account_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "account_label": "Main Checking",
    "status": "booked",
    "amount": 1250.0,
    "currency": "EUR"
  },
  "meta": {}
}
```

### Error Event Example

When access expires or is revoked, the source emits an actionable error event so your sinks can notify you:

```json
{
  "event_type": "nordigen.error.access_expired",
  "entity_id": null,
  "data": {
    "account_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "source": "nordigen_checking",
    "summary": "AccessExpiredError",
    "detail": "Access has expired or it has been revoked.",
    "action": "Reconnect the bank account using: python main.py nordigen connect"
  }
}
```

The `action` field tells you exactly what to do. Non-actionable errors (institution unavailable, rate limits) are only logged — they are not emitted as events.

## CLI Reference

### `nordigen auth`

Exchanges your `secret_id` and `secret_key` for a long-lived refresh token. Run once.

```
python main.py nordigen auth [--secret-id ID] [--secret-key KEY]
```

Options are read from `NORDIGEN_SECRET_ID` / `NORDIGEN_SECRET_KEY` env vars if not provided.

### `nordigen connect`

Interactive wizard to connect a bank account and add it to `config.yaml`.

```
python main.py nordigen connect --country COUNTRY [OPTIONS]
```

| Option           | Default                          | Description                                                          |
|:-----------------|:---------------------------------|:---------------------------------------------------------------------|
| `--country`      | `GB`                             | ISO 3166-1 alpha-2 country code (e.g. `CZ`, `DE`, `GB`).            |
| `--source-name`  | `nordigen_{account_id[:8]}`      | Name of the source entry written to `config.yaml`.                   |
| `--config-file`  | `config.yaml`                    | Path to your config file.                                            |
| `--history-days` | `90`                             | Days of transaction history to request (bank-dependent max).         |
| `--redirect-url` | `https://example.com/callback`   | Redirect URL shown after bank authentication (any valid URL works).  |

## Troubleshooting

**`nordigen.error.access_expired` event received** — Your consent has expired or been revoked. Re-run `nordigen connect` to reconnect the account.

**`nordigen.error.access_forbidden` event received** — The account may not have the necessary permissions. Re-run `nordigen connect` or check your GoCardless dashboard.

**"Authentication failed (HTTP 401)"** — Your `secret_id` / `secret_key` are wrong, or your refresh token has expired (~30 days). Re-run `nordigen auth` to mint a new refresh token.

**"No banks found for country"** — Check the country code. It must be a two-letter ISO code (e.g. `CZ`, not `Czech Republic`).

**Rate limit warnings in logs** — Normal if you recently restarted. The source backs off automatically for 6 hours and resumes. Do not lower `poll_interval` below `"6h"`.

**Pending transactions disappear** — This is normal. Banks remove pending items when they settle (or cancel). The settled version appears as a new booked transaction.
