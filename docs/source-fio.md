# Fio Banka Source

The Fio Banka source polls the [Fio banka](https://www.fio.cz/) REST API for new transactions and emits events for each incoming or outgoing payment. It uses cursor-based sync to fetch only new transactions on each poll.

Use this source to automate financial workflows: track incoming payments for order fulfillment, monitor expenses, categorize spending, or get instant notifications for account movements.

## Getting Started

### 1. Generate an API token

1. Log in to your Fio Internet Banking.
2. Go to **Settings** → **API** tab.
3. Click **Add Token** and choose **Account Monitoring** (Sledování účtu).
4. Set the validity (max 180 days, can be auto-extended).
5. Authorize the request (SMS/Push).
6. Copy the 64-character token.

*Note: Newly created tokens take about 5 minutes to become active.*

### 2. Add the source to `config.yaml`

You can provide the token directly or via the `FIO_TOKEN` environment variable:

```yaml
sources:
  my_fio:
    type: fio
    token: "YOUR_64_CHAR_TOKEN"
```

Or with the environment variable:

```yaml
sources:
  my_fio:
    type: fio
```

## Core Concepts

### Rate Limiting

The Fio API allows one request per 30 seconds per token. The source includes an internal safety mechanism that enforces a minimum of 35 seconds between requests, regardless of the configured `poll_interval`.

### Cursor-Based Sync

The source stores the date of the last successful sync. On each poll, it fetches transactions from that date up to `look_ahead_days` into the future. Transactions are deduplicated by their unique Fio `IDpohybu`, so overlapping poll windows don't produce duplicate events.

## Configuration

### Minimal Configuration

```yaml
sources:
  my_fio:
    type: fio
```

Defaults: `poll_interval: "30m"`, `max_days_back: 15`, `look_ahead_days: 5`.

### Full Configuration

```yaml
sources:
  my_fio:
    type: fio
    token: "YOUR_64_CHAR_TOKEN"
    poll_interval: "15m"
    max_days_back: 60
    look_ahead_days: 14
```

### Configuration Reference

| Parameter         | Type     | Default | Description                                                                  |
|:------------------|:---------|:--------|:-----------------------------------------------------------------------------|
| `token`           | `string` | Env var | Fio API token. Defaults to `FIO_TOKEN` environment variable.                 |
| `poll_interval`   | `string` | `"30m"` | How often to check for new transactions. Supports human-readable intervals.  |
| `max_days_back`   | `int`    | `15`    | How far back to look on the very first sync.                                 |
| `look_ahead_days` | `int`    | `5`     | How many days into the future to look for pending/future-dated transactions. |

## Event Definitions

| Type                      | Entity ID      | Description                                          |
|:--------------------------|:---------------|:-----------------------------------------------------|
| `fio.transaction.income`  | Account Number | Incoming transaction (amount > 0).                   |
| `fio.transaction.expense` | Account Number | Outgoing transaction (amount < 0).                   |
| `fio.transaction`         | Account Number | Transaction with amount exactly 0 (rare).            |

### Event Example

```json
{
  "id": 1,
  "event_id": "1148734530",
  "event_type": "fio.transaction.income",
  "entity_id": "2400222222",
  "created_at": "2024-03-15T10:00:00+00:00",
  "data": {
    "id": "1148734530",
    "date": "2024-03-15",
    "amount": 1250.0,
    "currency": "CZK",
    "counterpart_account": "2900233333",
    "counterpart_name": "Pavel Novák",
    "counterpart_bank_code": "2010",
    "counterpart_bank_name": "Fio banka, a.s.",
    "variable_symbol": "2024001",
    "constant_symbol": "0308",
    "recipient_message": "Invoice payment",
    "type": "Příjem převodem uvnitř banky",
    "instruction_id": "2105685816",
    "account_id": "2400222222",
    "bank_id": "2010",
    "balance": 15420.50
  },
  "meta": {}
}
```

- `id`: The unique `IDpohybu` from Fio.
- `balance`: Account balance *after* this transaction.
- `instruction_id`: The ID of the original instruction (`ID pokynu`).
- `type`: Human-readable transaction type in Czech (as provided by Fio).
