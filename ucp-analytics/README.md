# UCP Analytics — Sample Implementation

A single-file reference for capturing
[Universal Commerce Protocol](https://ucp.dev) (UCP) traffic from an
[httpx](https://www.python-httpx.org/) client and streaming events
into a partitioned, clustered BigQuery table.

This is a **sample**, not a framework. The whole adapter is
`ucp_analytics.py` (~550 lines). Read it, copy it into your project,
and edit the schema / classification rules to fit your workload.

## Install

```bash
git clone https://github.com/haiyuan-eng-google/data-agent-kit.git
cd data-agent-kit/ucp-analytics
pip install -e .
```

## Usage

```python
import asyncio
import httpx
from ucp_analytics import BQWriter, UCPTracker, make_event_hook

async def main():
    writer = BQWriter(
        project_id="my-gcp-project",
        dataset_id="ucp_analytics",
        table_id="ucp_events",
    )
    tracker = UCPTracker(writer)

    async with httpx.AsyncClient(
        event_hooks={"response": [make_event_hook(tracker)]},
    ) as client:
        await client.get("https://merchant.example.com/.well-known/ucp")
        await client.post(
            "https://merchant.example.com/checkout-sessions",
            json={"line_items": [{"item": {"id": "roses"}, "quantity": 1}]},
        )

    await writer.close()  # drain + close

asyncio.run(main())
```

Every UCP request through that client produces one BigQuery row.

## Smoke test (no GCP credentials needed)

```bash
python ucp_analytics.py
```

Runs five synthetic responses through the parser and prints the
resulting rows as JSON lines so you can verify the
classifier / extractor without hitting BigQuery.

## What it does

| Surface | Code |
|---|---|
| Path-based classifier (checkout / cart / catalog / order / identity / discovery / error / fallback) | `classify(method, path, status)` |
| Body-field extractor (id, currency, total amount, first error code) | `extract_fields(body)` |
| Async buffered streaming insert into a partitioned, clustered table | `BQWriter` |
| `httpx` response event hook gluing the two together | `UCPTracker.record` + `make_event_hook` |

Schema is 13 columns, defined as a list of tuples at the top of
`ucp_analytics.py`. Add columns there when your dashboards need them.

## What it doesn't do (fork for any of this)

- FastAPI / Starlette server-side middleware
- Google ADK plugin
- MCP / A2A / JSON-RPC transport handling
- HTTP message signing (RFC 9421) parsing
- `WWW-Authenticate` Bearer challenge parsing (RFC 7235 / 6750)
- Standard Webhooks header-pair detection and lifecycle derivation
  from `fulfillment.events[]` / `adjustments[]`
- AP2 mandates, authorization signals, embedded checkout config,
  OAuth identity-linking discovery
- PII redaction
- Per-PR / per-merchant column splits

The classifier is deliberately a flat list of substring checks; the
extractor knows about exactly four body fields. Both are easy to
read and easy to grow.

## License

[Apache 2.0](../LICENSE), inherited from the Data Agent Kit
repository.
