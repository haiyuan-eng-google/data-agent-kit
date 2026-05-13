# UCP Analytics — Sample Implementation

A single-file reference for capturing
[Universal Commerce Protocol](https://ucp.dev) (UCP) traffic from an
[httpx](https://www.python-httpx.org/) client and streaming events
into a partitioned, clustered BigQuery table.

This is a **sample**, not a framework. The whole adapter is
`ucp_analytics.py` (~1200 lines including docstrings + smoke test;
under 1000 SLOC). Read it, copy it into your project, and edit the
schema / classification rules to fit your workload. Licensed
[Apache 2.0](../LICENSE), Copyright 2026 Google LLC.

Covers all 32 event types in the UCP spec: 27 derivable from HTTPX
traffic by the parser; 6 emitted by your agent loop via the included
`SampleAgent` shape; `order_webhook_received` overlaps both surfaces
(parser sees inbound POSTs; agent is the entry point for
server-side webhook handlers). 27 + 6 − 1 = 32.

## Install

```bash
git clone https://github.com/haiyuan-eng-google/data-agent-kit.git
cd data-agent-kit/ucp-analytics

# Only two runtime deps; install them however your project does it.
pip install httpx google-cloud-bigquery

# Then drop the script into your project, or run it from here.
python ucp_analytics.py
```

There is intentionally no `pyproject.toml` and no install step — this
is a single file you read and copy, not a package you depend on.

## Usage

```python
import asyncio
import httpx
from ucp_analytics import (
    BQWriter, UCPTracker, SampleAgent, make_event_hook,
)

async def main():
    writer = BQWriter(
        project_id="my-gcp-project",
        dataset_id="ucp_analytics",
        table_id="ucp_events",
    )
    tracker = UCPTracker(writer)
    agent = SampleAgent(tracker)

    async with httpx.AsyncClient(
        event_hooks={"response": [make_event_hook(tracker)]},
    ) as client:
        # HTTPX traffic — parser derives 27 event types.
        await client.get("https://merchant.example.com/.well-known/ucp")
        await client.post(
            "https://merchant.example.com/checkout-sessions",
            json={"line_items": [{"item": {"id": "roses"}, "quantity": 1}]},
        )

    # Agent-decision moments — 6 SampleAgent calls (5 unique types
    # plus an overlap on order_webhook_received with the parser).
    # These don't pass through HTTPX, so the parser can't see them.
    await agent.capability_negotiated(merchant_host="merchant.example.com")
    await agent.payment_completed(
        checkout_session_id="chk_abc", currency="USD", total_amount=3249,
    )

    await writer.close()  # drain + close

asyncio.run(main())
```

Every UCP request through that client produces one BigQuery row; so
does every `SampleAgent` call.

## Smoke test (no GCP credentials needed)

```bash
python ucp_analytics.py
```

Drives 33 synthetic events through the full pipeline (parser +
agent), prints each row as a JSON line, and asserts that every one
of the 32 UCP event types appears at least once. Exits non-zero if
coverage regresses.

## End-to-end against real BigQuery

```bash
# Auth once (uses Application Default Credentials).
gcloud auth application-default login

# Stream the same 33 rows into BigQuery and verify they're queryable.
python ucp_analytics.py \
    --e2e \
    --project-id YOUR_GCP_PROJECT \
    --verify

# Or without --verify if you just want to write and not wait:
python ucp_analytics.py --e2e --project-id YOUR_GCP_PROJECT
```

What `--e2e` does:

1. Generates a unique `merchant_host` tag (`smoke-{uuid}.example.com`)
   so this run's rows are filterable in the table.
2. Auto-creates the dataset (default `ucp_analytics_e2e`) and table
   (default `ucp_events_smoke`) on first run, partitioned by
   `timestamp` and clustered by `(event_type, checkout_session_id)`.
3. Streams all 33 rows through `BQWriter` and drains the buffer.
4. With `--verify`, polls the table with backoff (up to
   `--verify-timeout` seconds, default 90) until every row from this
   run is queryable, then prints `verify ok`.

The auto-created table is the same schema (13 columns) the live
`BQWriter` writes to in your own integration. Drop the dataset when
you're done — it's tagged `_e2e` for that reason.

## What it does

| Surface | Code |
|---|---|
| Path-based classifier (checkout / cart / catalog / order / identity / discovery / webhook / error / fallback) | `classify(method, path, status, body)` |
| Body-derived order lifecycle (`shipped` / `delivered` / `returned` / `canceled` from `fulfillment.events[]` or `adjustments[]`) | `_lifecycle_from_body(body)` |
| Body-field extractor (id, currency, total amount, first error code) | `extract_fields(body)` |
| Async buffered streaming insert into a partitioned, clustered table | `BQWriter` |
| `httpx` response event hook gluing the parser to the writer | `UCPTracker.record` + `make_event_hook` |
| Manual entry point for non-HTTPX events | `UCPTracker.record_event` |
| Reference shape for an agent's analytics emission (payment, capability negotiation, webhook receipts) | `SampleAgent` |

Schema is 13 columns, defined as a list of tuples at the top of
`ucp_analytics.py`. Add columns there when your dashboards need them.

### Event coverage

The parser derives 27 event types from HTTPX responses (path +
method + status + body); `SampleAgent` (or your own
`tracker.record_event` calls) emits the other 5 plus an overlap on
`order_webhook_received`:

| Source | Events |
|---|---|
| HTTPX parser (27) | `profile_discovered`, `checkout_session_{created,get,updated,completed,canceled}`, `checkout_escalation`, `cart_{created,get,updated,canceled}`, `catalog_{search,lookup,product_get}`, `order_{created,get,updated,shipped,delivered,returned,canceled,webhook_received}`, `identity_link_{initiated,completed,revoked}`, `error`, `request` |
| `SampleAgent` (5 unique + 1 overlap) | `capability_negotiated`, `payment_handler_negotiated`, `payment_instrument_selected`, `payment_completed`, `payment_failed`, **`order_webhook_received`** (overlaps with parser — use whichever path your handler runs in) |

Total distinct: 32. The smoke test emits 33 rows (27 parser + 6
agent) and asserts all 32 distinct types appear.

## What it doesn't do (fork for any of this)

- FastAPI / Starlette server-side middleware
- Google ADK plugin
- MCP / A2A / JSON-RPC transport handling
- HTTP message signing (RFC 9421) parsing
- `WWW-Authenticate` Bearer challenge parsing (RFC 7235 / 6750)
- Standard Webhooks header-pair signature verification
- AP2 mandates, authorization signals, embedded checkout config
- PII redaction
- Per-PR / per-merchant column splits

The classifier is deliberately a flat list of substring checks; the
extractor knows about exactly four body fields. Both are easy to
read and easy to grow.

## License

[Apache 2.0](../LICENSE), inherited from the Data Agent Kit
repository.
