# Faktury Online Source

The Faktury Online source monitors your [Faktury-online.com](https://www.faktury-online.com/) account (a Slovak invoicing service) and emits events when invoices are created, updated, or deleted. It uses periodic polling with local state caching to detect changes.

Use this source to keep your accounting or ERP system in sync with your invoices — automatically pull new invoices, react when an invoice is marked as paid, or detect deletions.

## Getting Started

### 1. Set up credentials

Provide your API key and email either in `config.yaml` or via environment variables:

- `FAKTURY_ONLINE_KEY` — your 32-character API key from Faktury Online.
- `FAKTURY_ONLINE_EMAIL` — your login email.

### 2. Add the source to `config.yaml`

With environment variables:

```yaml
sources:
  my_invoices:
    type: faktury_online
```

Or with credentials in config:

```yaml
sources:
  my_invoices:
    type: faktury_online
    api_key: "your-32-char-api-key"
    email: "your-email@example.com"
```

## Core Concepts

### Change Detection

The source maintains a local cache of each invoice's last known state. On each poll, it fetches recently created invoices and compares them against the cache:

- **New invoice** → `faktury.invoice.created`
- **Changed fields** → `faktury.invoice.updated` (includes before/after values for each changed field)
- **Invoice no longer found** → `faktury.invoice.deleted`

## Configuration

### Minimal Configuration

```yaml
sources:
  my_invoices:
    type: faktury_online
```

Defaults: `poll_interval: "6h"`, `max_days_back: 30`.

### Full Configuration

```yaml
sources:
  my_invoices:
    type: faktury_online
    api_key: "your-32-char-api-key"
    email: "your-email@example.com"
    poll_interval: "15m"
    max_days_back: 60
```

### Configuration Reference

| Parameter       | Type     | Default | Description                                                                                   |
|:----------------|:---------|:--------|:----------------------------------------------------------------------------------------------|
| `api_key`       | `string` | Env var | 32-character API key. Defaults to `FAKTURY_ONLINE_KEY` environment variable.                  |
| `email`         | `string` | Env var | Login email. Defaults to `FAKTURY_ONLINE_EMAIL` environment variable.                         |
| `poll_interval` | `string` | `"6h"`  | How often to check for changes. Supports human-readable intervals (e.g. `"15m"`, `"1h"`).     |
| `max_days_back` | `int`    | `30`    | How many days back to look for created/updated invoices during each poll.                     |

## Event Definitions

| Type                       | Entity ID    | Description                                               |
|:---------------------------|:-------------|:----------------------------------------------------------|
| `faktury.invoice.created`  | Invoice Code | A new invoice was discovered.                             |
| `faktury.invoice.updated`  | Invoice Code | An existing invoice's properties changed.                 |
| `faktury.invoice.deleted`  | Invoice Code | An invoice is no longer found in the poll results.        |

### Event Examples

#### `faktury.invoice.created`

Contains the full invoice detail as returned by the Faktury Online API:

```json
{
  "id": 1,
  "event_id": "faktury-2024-001-created",
  "event_type": "faktury.invoice.created",
  "entity_id": "2024-001",
  "created_at": "2024-03-15T10:00:00+00:00",
  "data": {
    "invoice": {
      "invoice_number": "2024-001",
      "supplier": "My Company s.r.o.",
      "customer": "John Doe",
      "invoice_amount": 120.50,
      "invoice_currency": "EUR",
      "invoice_paid": "nie"
    }
  },
  "meta": {}
}
```

#### `faktury.invoice.updated`

Contains the specific changes and the new full snapshot:

```json
{
  "id": 2,
  "event_id": "faktury-2024-001-updated-1710500000",
  "event_type": "faktury.invoice.updated",
  "entity_id": "2024-001",
  "created_at": "2024-03-15T12:00:00+00:00",
  "data": {
    "changes": {
      "invoice_paid": { "before": "nie", "after": "ano" },
      "invoice_paid_amount": { "before": 0, "after": 120.50 }
    },
    "snapshot": {
      "invoice_number": "2024-001",
      "invoice_paid": "ano"
    }
  },
  "meta": {}
}
```

#### `faktury.invoice.deleted`

Contains the last known state of the invoice:

```json
{
  "id": 3,
  "event_id": "faktury-2024-001-deleted",
  "event_type": "faktury.invoice.deleted",
  "entity_id": "2024-001",
  "created_at": "2024-03-15T14:00:00+00:00",
  "data": {
    "last_known_state": {
      "invoice_number": "2024-001"
    }
  },
  "meta": {}
}
```
