# Universal Commerce Protocol Analytics

**BigQuery-backed commerce analytics for the
[Universal Commerce Protocol (UCP)](https://ucp.dev).**

[Documentation](https://ucp.dev) |
[Specification](https://ucp.dev/specification/overview) |
[Discussions](https://github.com/Universal-Commerce-Protocol/ucp/discussions)

## Overview

UCP defines the protocol for agentic commerce — but ships no observability.
This module automatically captures checkout sessions, order lifecycle,
payment flows, capability negotiation, and identity linking events into
BigQuery for funnel analysis, error debugging, latency monitoring, and
revenue attribution. It ships as part of the
[Data Agent Kit](https://github.com/haiyuan-eng-google/data-agent-kit) monorepo
under `ucp-analytics/`.

Three integration points — pick any or combine:

| Integration | Side | One-liner |
|---|---|---|
| **FastAPI middleware** | Merchant server | `app.add_middleware(UCPAnalyticsMiddleware, tracker=t)` |
| **HTTPX event hook** | Agent / platform | `httpx.AsyncClient(event_hooks={"response": [hook]})` |
| **ADK plugin** *(optional)* | Google ADK agent | `Runner(plugins=[UCPAgentAnalyticsPlugin(...)])` |

```
 Platform (Agent)                    Business (Merchant)
 ┌───────────────┐                   ┌───────────────────┐
 │ httpx client   │                   │ FastAPI server     │
 │ + EventHook  ─────── REST ──────────► + Middleware     │
 └───────┬───────┘                   └────────┬──────────┘
         │                                    │
         └──────────┬─────────────────────────┘
                    ▼
           UCPAnalyticsTracker
           ├── UCPResponseParser   (classify + extract)
           └── AsyncBigQueryWriter (batch + flush)
                    │
                    ▼
               BigQuery
          PARTITION BY timestamp
          CLUSTER BY event_type,
            checkout_session_id,
            merchant_host
```

## Installation

Install from source by cloning the Data Agent Kit repository and using the
`ucp-analytics/` folder directly:

```bash
git clone https://github.com/haiyuan-eng-google/data-agent-kit.git
cd data-agent-kit/ucp-analytics
```

Then install via pip (editable so source edits take effect immediately):

```bash
pip install -e .                  # Core (tracker + HTTPX hook)
pip install -e ".[fastapi]"       # With FastAPI middleware
pip install -e ".[adk]"           # With Google ADK plugin adapter
pip install -e ".[fastapi,adk]"   # All extras
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv sync              # Core
uv sync --all-extras # All extras
```

## Quick Start

### Merchant server (FastAPI)

Add two lines to your UCP `server.py`:

```python
import os
from ucp_analytics import UCPAnalyticsTracker, UCPAnalyticsMiddleware

tracker = UCPAnalyticsTracker(
    project_id=os.environ.get("GCP_PROJECT_ID", "your-gcp-project-id"),
    app_name="flower_shop",
)
app.add_middleware(UCPAnalyticsMiddleware, tracker=tracker)

@app.on_event("shutdown")
async def shutdown():
    await tracker.close()  # drains in-flight tasks, then flushes
```

> **Note:** `UCPAnalyticsMiddleware` requires the `[fastapi]` extra.
> The middleware is lazy-loaded so the core module works without starlette installed.

### Agent / platform client (HTTPX)

```python
import os
import httpx
from ucp_analytics import UCPAnalyticsTracker, UCPClientEventHook

tracker = UCPAnalyticsTracker(
    project_id=os.environ.get("GCP_PROJECT_ID", "your-gcp-project-id"),
    app_name="shopping_agent",
)
hook = UCPClientEventHook(tracker)

client = httpx.AsyncClient(event_hooks={"response": [hook]})
```

Every call to `/checkout-sessions`, `/.well-known/ucp`, `/orders`, `/carts`,
`/webhooks`, etc. is automatically classified and written to BigQuery.

## Events Tracked

Every UCP event type is auto-classified from HTTP method + path + response body. The canonical list lives in [`src/ucp_analytics/events.py::UCPEventType`](src/ucp_analytics/events.py); the groups below are the categorical view.

### Checkout (6)

| HTTP Operation | Event Type |
|---|---|
| `POST /checkout-sessions` | `checkout_session_created` |
| `GET /checkout-sessions/{id}` | `checkout_session_get` |
| `PUT /checkout-sessions/{id}` | `checkout_session_updated` |
| `PUT /checkout-sessions/{id}` *(status=requires_escalation)* | `checkout_escalation` |
| `POST /checkout-sessions/{id}/complete` | `checkout_session_completed` |
| `POST /checkout-sessions/{id}/cancel` | `checkout_session_canceled` |

### Cart (4)

| HTTP Operation | Event Type |
|---|---|
| `POST /carts` | `cart_created` |
| `GET /carts/{id}` | `cart_get` |
| `PUT /carts/{id}` | `cart_updated` |
| `POST /carts/{id}/cancel` | `cart_canceled` |

### Catalog (3)

| HTTP Operation | Event Type |
|---|---|
| `POST /catalog/search` | `catalog_search` |
| `POST /catalog/lookup` | `catalog_lookup` |
| `POST /catalog/product` | `catalog_product_get` |

### Order (8)

Order lifecycle at UCP `c5c6139` derives from `fulfillment.events[]` and `adjustments[]`; legacy top-level `status` is the fallback for pre-c5c6139 senders.

| HTTP Operation | Event Type |
|---|---|
| `POST /orders` | `order_created` |
| `GET /orders/{id}` *(no lifecycle)* | `order_get` |
| `PUT /orders/{id}` *(no lifecycle)* | `order_updated` |
| `GET`/`PUT` `/orders/{id}` *(latest fulfillment event is shipped or in_transit)* | `order_shipped` |
| `GET`/`PUT` `/orders/{id}` *(...is delivered)* | `order_delivered` |
| `GET`/`PUT` `/orders/{id}` *(...is returned_to_sender, or adjustments[].type is refund/return)* | `order_returned` |
| `GET`/`PUT` `/orders/{id}` *(...is canceled/undeliverable, or adjustments[].type is cancellation)* | `order_canceled` |
| Webhook delivery (path or `Webhook-Id` + `Webhook-Timestamp` header pair) without recognizable lifecycle | `order_webhook_received` |

### Identity (3)

| HTTP Operation | Event Type |
|---|---|
| `POST /identity` | `identity_link_initiated` |
| `GET /identity/callback` | `identity_link_completed` |
| `POST /identity/revoke` | `identity_link_revoked` |

### Payment (4)

| Event Type | Description |
|---|---|
| `payment_handler_negotiated` | Platform + merchant handler intersection computed |
| `payment_instrument_selected` | Buyer selects payment instrument |
| `payment_completed` | Payment succeeds |
| `payment_failed` | Payment fails |

### Discovery (2)

| HTTP Operation | Event Type |
|---|---|
| `GET /.well-known/ucp` | `profile_discovered` |
| Capability exchange | `capability_negotiated` |

### Fallback (2)

| Condition | Event Type |
|---|---|
| Any unmatched path, status >= 400 | `error` |
| Any unmatched path, status < 400 | `request` |

Webhook paths use the **request body** (order payload) for classification since
the response is typically an ack. Webhook 4xx/5xx responses classify as `error`.

## Configuration

```python
import os

UCPAnalyticsTracker(
    project_id=os.environ.get("GCP_PROJECT_ID"),  # required — GCP project
    dataset_id="ucp_analytics",     # BigQuery dataset
    table_id="ucp_events",          # BigQuery table
    app_name="flower_shop",         # tags every event
    batch_size=50,                  # flush every N events
    auto_create_table=True,         # create table on first write
    redact_pii=False,               # redact email, phone, address
    custom_metadata={"env": "prod"},
)
```

The underlying `AsyncBigQueryWriter` also accepts `max_buffer_size`
(default: 10,000) to cap in-memory buffering when BigQuery is unreachable.

**BigQuery schema notes (v0.2 spec alignment):** The schema uses `fulfillment_amount`
(replacing the earlier `shipping_amount`) to align with UCP spec total types. Additional
fields include `items_discount_amount`, `fee_amount`, `discount_codes_json`,
`discount_applied_json` (discount extension), `expires_at`, `continue_url`
(checkout metadata), and `permalink_url` (order permalink).

## Examples

Eight runnable examples are included — see [`examples/README.md`](examples/README.md) for full details.

| Example | BigQuery? | Transport | Coverage |
|---|---|---|---|
| [`e2e_demo.py`](examples/e2e_demo.py) | No (SQLite) | REST | Checkout happy path (5 types) |
| [`scenarios_demo.py`](examples/scenarios_demo.py) | Yes | REST | Errors, cancellation, escalation (7 types) |
| [`cart_demo.py`](examples/cart_demo.py) | Yes | REST | Cart CRUD + checkout conversion (6 types) |
| [`order_lifecycle_demo.py`](examples/order_lifecycle_demo.py) | Yes | REST | Order delivered/returned/canceled (8 types) |
| [`transport_demo.py`](examples/transport_demo.py) | Yes | REST/MCP/A2A | All 3 transports compared (5 types) |
| [`identity_payment_demo.py`](examples/identity_payment_demo.py) | Yes | REST | Identity linking + payment flows (10 types) |
| [`bq_demo.py`](examples/bq_demo.py) | Yes | REST/MCP/A2A | Every event type, 3 transports, BQ verification |
| [`bq_adk_demo.py`](examples/bq_adk_demo.py) | Yes | ADK/MCP/A2A | Every event type via ADK plugin, BQ verification |

Shared configuration lives in [`examples/_demo_utils.py`](examples/_demo_utils.py).
Set `GCP_PROJECT_ID` in your environment or edit the file directly.

## Dashboard Queries

See [`dashboards/queries.sql`](dashboards/queries.sql) for 10 ready-to-use
BigQuery queries: checkout funnel, revenue by merchant, payment handler mix,
capability adoption, error analysis, escalation rate, latency percentiles,
fulfillment geography, session timeline, and discovery-to-checkout rate.

## Folder Structure

```
data-agent-kit/ucp-analytics/
├── src/ucp_analytics/
│   ├── __init__.py                 # public API exports (lazy-loads middleware)
│   ├── events.py                   # UCPEvent, UCPEventType, CheckoutStatus
│   ├── parser.py                   # classify HTTP→event, extract fields
│   ├── writer.py                   # AsyncBigQueryWriter (batch + DDL)
│   ├── tracker.py                  # UCPAnalyticsTracker (orchestrator)
│   ├── middleware.py               # FastAPI/Starlette ASGI middleware
│   ├── client_hooks.py             # HTTPX event hook for agent clients
│   └── adk_plugin.py              # optional ADK BasePlugin adapter
├── tests/
│   ├── test_parser.py              # classify + extract unit tests
│   ├── test_events.py              # UCPEvent + enum tests
│   ├── test_tracker.py             # tracker + PII redaction tests
│   ├── test_writer.py              # buffer, flush, retry, DDL tests
│   └── test_client_hooks.py        # HTTPX hook tests
├── examples/
│   ├── _demo_utils.py              # shared BQ config (PROJECT_ID, helpers)
│   ├── e2e_demo.py                 # self-contained demo (no GCP)
│   ├── scenarios_demo.py           # error paths + edge cases
│   ├── cart_demo.py                # cart lifecycle
│   ├── order_lifecycle_demo.py     # order lifecycle
│   ├── transport_demo.py           # REST vs MCP vs A2A
│   ├── identity_payment_demo.py    # identity + payment flows
│   ├── bq_demo.py                  # comprehensive BQ demo (every event type)
│   ├── bq_adk_demo.py             # comprehensive ADK demo (every event type)
│   └── README.md                   # example guide with run instructions
├── dashboards/queries.sql          # 10 BigQuery analytics queries
├── docs/
│   ├── design_doc.md               # design document
│   └── bigquery-ucp-analytics.md   # BigQuery schema + usage guide
├── pyproject.toml                  # hatchling + uv + ruff
└── uv.lock                        # pinned dependencies
```

## Contributing

We welcome community contributions. See the UCP
[Contribution Guide](https://github.com/Universal-Commerce-Protocol/ucp/blob/main/CONTRIBUTING.md)
for details.

## License

Distributed under the [Apache License 2.0](../LICENSE), inherited from the
Data Agent Kit repository.
