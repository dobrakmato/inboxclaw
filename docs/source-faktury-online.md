# Faktury Online Source

The Faktury Online source provides a robust integration with [Faktury-online.com](https://www.faktury-online.com/), a Slovak invoicing service. This source allows you to monitor your invoices and react to changes such as new invoices, updates to existing ones (e.g., when they are paid), or deletions.

By monitoring your Faktury Online account, this source ensures your application stays in sync with your financial documents. It's particularly useful for:
- **ERP Integration**: Automatically pull new invoices into your internal accounting or ERP system.
- **Payment Notifications**: React instantly when an invoice status changes to "paid".
- **CRM Sync**: Keep customer billing history up to date in your CRM.

## Implementation Details

The Faktury Online source uses a periodic polling mechanism to check for changes in your invoices.

- **Session-Based API**: The source handles session initialization and cookie management required by the Faktury Online API.
- **State Caching**: To detect changes, the source maintains a local cache (using the `SourceKV` service) of the last known state of each invoice.
- **Difference Computation**: When an invoice is updated, the source computes exactly what changed and emits an event containing both the specific changes and the new full snapshot.
- **Time-Based Polling**: You can configure how far back the source should look for changes (e.g., last 30 days) to balance between completeness and performance.

## Core Concepts

- **Polling**: The source periodically calls the Faktury Online API to fetch a list of recently created invoices.
- **Change Detection**: By comparing the fetched data with the local cache, the source classifies changes into `created`, `updated`, or `deleted`.

## Configuration

### Minimal Configuration

Ensure you have your API key and email set in your environment variables:
- `FAKTURY_ONLINE_KEY`
- `FAKTURY_ONLINE_EMAIL`

```yaml
sources:
  my_invoices:
    type: "faktury_online"
```

### Full Configuration

```yaml
sources:
  my_invoices:
    type: "faktury_online"
    api_key: "your-32-char-api-key"
    email: "your-email@example.com"
    poll_interval: "15m"
    max_days_back: 60
```

### Configuration Parameters

| Parameter       | Type     | Default | Description                                                                                             |
|:----------------|:---------|:--------|:--------------------------------------------------------------------------------------------------------|
| `api_key`       | `string` | Env var | Your 32-character API key from Faktury Online. Defaults to `FAKTURY_ONLINE_KEY` environment variable.     |
| `email`         | `string` | Env var | Your login email for Faktury Online. Defaults to `FAKTURY_ONLINE_EMAIL` environment variable.            |
| `poll_interval` | `string` | `6h`    | How often to check for changes (e.g., "5m", "1h").                                                      |
| `max_days_back` | `integer`| `30`    | How many days back to look for created/updated invoices during each poll.                               |

## Event Definitions

| Type                       | Entity ID    | Description                                                   |
|:---------------------------|:-------------|:--------------------------------------------------------------|
| `faktury.invoice.created`  | Invoice Code | Triggered when a new invoice is discovered.                   |
| `faktury.invoice.updated`  | Invoice Code | Triggered when an existing invoice's properties change.       |
| `faktury.invoice.deleted`  | Invoice Code | Triggered when an invoice is no longer found in the poll.     |

### Data Payload Examples

#### `faktury.invoice.created`
Contains the full invoice detail as returned by the `/api/status` endpoint.
```json
{
  "invoice": {
    "invoice_number": "2024-001",
    "supplier": "My Company s.r.o.",
    "customer": "John Doe",
    "invoice_amount": 120.50,
    "invoice_currency": "EUR",
    "invoice_paid": "nie",
    ...
  }
}
```

#### `faktury.invoice.updated`
Contains the changes detected and the new full snapshot.
```json
{
  "changes": {
    "invoice_paid": {
      "before": "nie",
      "after": "ano"
    },
    "invoice_paid_amount": {
      "before": 0,
      "after": 120.50
    }
  },
  "snapshot": {
    "invoice_number": "2024-001",
    "invoice_paid": "ano",
    ...
  }
}
```

#### `faktury.invoice.deleted`
Contains the last known state of the invoice before it was removed.
```json
{
  "last_known_state": {
    "invoice_number": "2024-001",
    ...
  }
}
```
