# Fio Banka Source

The Fio Banka source allows the ingest pipeline to periodically poll for new transactions on a Fio bank account using their REST API.

## Why use this source?

This source is ideal for automating financial workflows, such as:
- Tracking incoming payments for automated order fulfillment.
- Monitoring business expenses in real-time.
- Categorizing personal spending for budgeting apps.
- Getting instant notifications for any account movement.

## Core Concepts

### Periodic Polling
Unlike real-time sources, this source periodically asks the Fio API for new data. By default, it checks every 10 minutes.

### Cursor-Based Synchronization
The source maintains a "cursor" (a date) to keep track of the last time it successfully synchronized. This ensures that only new transactions are processed and historical data isn't repeatedly fetched. On each poll, it fetches transactions from the last sync date up to a few days into the future.

### Unique Event IDs
Each transaction in Fio Banka has a unique `IDpohybu`. The source uses this as the `event_id`, which allows the pipeline to automatically deduplicate any transactions that might be reported multiple times (e.g., if a poll window overlaps).

### Internal Rate Limiting
Fio API has a strict rate limit of one request per 30 seconds per token. This source includes an internal safety mechanism that ensures it never makes requests more frequently than every 35 seconds.

## Configuration

### Minimal Configuration

To get started, you only need an API Token from your Fio Internet Banking. You can also provide the token via the `FIO_TOKEN` environment variable.

```yaml
sources:
  my_fio_account:
    type: fio
    # token: "YOUR_FIO_API_TOKEN" # Optional if FIO_TOKEN is set
```

### Full Configuration

You can customize how far back the source looks on the first run and how far into the future it looks for pending/future-dated transactions.

```yaml
sources:
  business_account:
    type: fio
    token: "..."
    poll_interval: "15m"      # How often to check for new transactions
    max_days_back: 60         # How far back to look on the very first sync
    look_ahead_days: 14       # How many days into the future to look
```

## Setup Guide

### 1. Generate an API Token
1. Log in to your Fio Internet Banking.
2. Click on **Settings** (top right corner).
3. Go to the **API** tab.
4. Click **Add Token**.
5. Choose **Account Monitoring** (Sledování účtu).
6. Set the validity (max 180 days, can be auto-extended).
7. Authorize the request (SMS/Push).
8. Copy the 64-character token.

*Note: It takes about 5 minutes for a newly created token to become active.*

## Event Definitions

| Type | Entity ID | Description |
| :--- | :--- | :--- |
| `fio.transaction.income` | Account Number | Triggered for any incoming transaction (amount > 0). |
| `fio.transaction.expense` | Account Number | Triggered for any outgoing transaction (amount < 0). |
| `fio.transaction` | Account Number | Triggered if the amount is exactly 0 (rare). |

### Data Payload

The `data` field contains all available information from the Fio API:

```json
{
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
}
```

- `id`: The unique `IDpohybu` from Fio.
- `balance`: The account balance **after** this transaction was processed.
- `instruction_id`: The ID of the original instruction (`ID pokynu`).
- `type`: Human-readable transaction type in Czech (as provided by Fio).
