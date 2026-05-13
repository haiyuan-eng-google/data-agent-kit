# UCP Analytics — Examples

This directory contains runnable examples that demonstrate UCP Analytics
covering every UCP event type across checkout, cart, order, identity,
payment, and transport scenarios.

## Getting the source

These examples live inside the
[Data Agent Kit](https://github.com/haiyuan-eng-google/data-agent-kit)
repository under `ucp-analytics/`. Clone and enter that folder before
running any of the snippets below:

```bash
git clone https://github.com/haiyuan-eng-google/data-agent-kit.git
cd data-agent-kit/ucp-analytics
```

All paths in this README are relative to `data-agent-kit/ucp-analytics/`.

## Overview

| Example | BigQuery? | Transport | Event Types Covered |
|---|---|---|---|
| [`e2e_demo.py`](#e2e-demo-local-sqlite) | No (SQLite) | REST | Checkout happy path (5 types) |
| [`scenarios_demo.py`](#scenarios-demo) | Yes (BigQuery) | REST | Errors, cancellation, escalation (7 types) |
| [`cart_demo.py`](#cart-demo) | Yes (BigQuery) | REST | Cart CRUD + checkout conversion (6 types) |
| [`order_lifecycle_demo.py`](#order-lifecycle-demo) | Yes (BigQuery) | REST | Order delivered/returned/canceled (8 types) |
| [`transport_demo.py`](#transport-demo) | Yes (BigQuery) | REST/MCP/A2A | All 3 transports compared (5 types) |
| [`identity_payment_demo.py`](#identity--payment-demo) | Yes (BigQuery) | REST | Identity linking + payment flows (10 types) |
| [`bq_demo.py`](#bigquery-demo) | Yes | REST/MCP/A2A | Comprehensive demo — every event type, 3 transports, SDK models, BQ verification |
| [`bq_adk_demo.py`](#adk-bigquery-demo) | Yes | ADK/MCP/A2A | Comprehensive ADK demo — every event type, 3 transports, SDK models, BQ verification |

### Quick Start (No GCP)

```bash
pip install fastapi uvicorn httpx
python examples/e2e_demo.py
```

### Quick Start (BigQuery)

```bash
gcloud auth application-default login
uv sync --all-extras
export GCP_PROJECT_ID="your-gcp-project-id"
uv run python examples/scenarios_demo.py    # errors + edge cases
uv run python examples/cart_demo.py          # cart lifecycle
uv run python examples/order_lifecycle_demo.py  # order lifecycle
uv run python examples/transport_demo.py     # REST vs MCP vs A2A
uv run python examples/identity_payment_demo.py  # identity + payment
```

### Prerequisites (BigQuery demos)

1. **Google Cloud project** with BigQuery API enabled:

   ```bash
   gcloud services enable bigquery.googleapis.com
   ```

2. **Application Default Credentials** (ADC):

   ```bash
   gcloud auth application-default login
   ```

3. **Python dependencies:**

   ```bash
   uv sync --all-extras
   ```

4. **Configuration:**

   Set the `GCP_PROJECT_ID` environment variable:

   ```bash
   export GCP_PROJECT_ID="your-gcp-project-id"
   ```

---

## BigQuery Demo

**`bq_demo.py`** — Comprehensive demo covering every UCP event type across
3 transports (REST, MCP, A2A) with UCP SDK models and BigQuery verification.

### What It Does

Spins up a mini UCP merchant server with all endpoints (checkout, cart, order,
identity, payment, capabilities) and a shopping agent client. Both sides are
instrumented with analytics. The demo runs in three phases:

1. **REST transport** — exercises every event type via HTTP (discovery, checkout
   lifecycle, cart lifecycle, order lifecycle, identity linking, payment events,
   capability negotiation, error/fallback)
2. **MCP transport** — replays key operations via `record_jsonrpc(transport="mcp")`
3. **A2A transport** — replays key operations via `record_jsonrpc(transport="a2a")`

After all phases, BigQuery is queried to verify every event type are present
across all 3 transports.

### Run

```bash
export GCP_PROJECT_ID="your-gcp-project-id"
uv run python examples/bq_demo.py
```

### Verify Manually

```sql
SELECT event_type, transport, COUNT(*) as cnt
FROM `YOUR_PROJECT.ucp_analytics.ucp_events`
WHERE app_name = 'bq_demo'
GROUP BY event_type, transport
ORDER BY event_type, transport;
```

---

## ADK BigQuery Demo

**`bq_adk_demo.py`** — Comprehensive ADK demo covering every UCP event type
across 3 transports (REST/ADK, MCP, A2A) with UCP SDK models and BigQuery
verification.

### What It Does

Uses `UCPAgentAnalyticsPlugin` for tool-based events and a separate
`UCPAnalyticsTracker` for events the plugin can't classify. Runs in five phases:

1. **Plugin tool calls** — 22 event types via `simulate_tool_call()` (discovery,
   catalog search/lookup/product, checkout lifecycle including escalation, cart
   lifecycle, order lifecycle through all terminal states + REST update +
   webhook receipt)
2. **Direct tracker events** — 10 event types the plugin can't handle (identity
   linking, payment flows, capability negotiation, error, request)
3. **MCP transport** — replays key operations via `record_jsonrpc(transport="mcp")`
4. **A2A transport** — replays key operations via `record_jsonrpc(transport="a2a")`
5. **Non-UCP tool** — verifies `get_weather` is correctly skipped

After all phases, BigQuery is queried to verify every event type are present.

### Run

```bash
export GCP_PROJECT_ID="your-gcp-project-id"
uv run python examples/bq_adk_demo.py
```

### Verify Manually

```sql
SELECT event_type, transport, COUNT(*) as cnt
FROM `YOUR_PROJECT.ucp_analytics.ucp_events`
WHERE app_name = 'bq_adk_demo'
GROUP BY event_type, transport
ORDER BY event_type, transport;
```

---

## Scenarios Demo

**`scenarios_demo.py`** — Exercises error paths and edge cases in UCP checkout.
Uses UCP SDK Pydantic models and writes to BigQuery.

```bash
uv run python examples/scenarios_demo.py
```

**Scenarios covered:**

1. **Payment failure + retry** — complete with `fail_token` (402), then `success_token`
2. **Fraud block** — complete with `fraud_token` (403)
3. **Out of stock** — create checkout with unavailable item (400)
4. **Checkout cancellation** — `POST /checkout-sessions/{id}/cancel`
5. **Escalation + recovery** — trigger `requires_escalation`, poll with GET, then complete
6. **404 Not Found** — GET nonexistent session
7. **Idempotency conflict** — duplicate POST with same key (409)

**Event types:** `checkout_session_created`, `checkout_session_updated`,
`checkout_session_completed`, `checkout_session_canceled`, `checkout_escalation`,
`checkout_session_get`, `error`

---

## Cart Demo

**`cart_demo.py`** — Full cart lifecycle including cart-to-checkout conversion.
Uses UCP SDK Pydantic models and writes to BigQuery.

```bash
uv run python examples/cart_demo.py
```

**Flows:**

1. **Cart CRUD** — create, get, update (add/remove items), get
2. **Cart cancellation** — cancel an active cart
3. **Cart-to-checkout conversion** — create cart, convert to checkout, complete

**Event types:** `cart_created`, `cart_get`, `cart_updated`, `cart_canceled`,
`checkout_session_created`, `checkout_session_completed`

---

## Order Lifecycle Demo

**`order_lifecycle_demo.py`** — Full order lifecycle through all terminal states.
Writes to BigQuery.

```bash
uv run python examples/order_lifecycle_demo.py
```

**Flows:**

1. **Happy path** — order created -> shipped -> delivered
2. **Cancellation** — order created -> canceled
3. **Return** — shipped -> delivered -> returned
4. **Fulfillment variants** — shipping, pickup, digital

**Event types:** `order_created`, `order_get`, `order_shipped`,
`order_delivered`, `order_returned`, `order_canceled`, `order_webhook_received`

---

## Transport Demo

**`transport_demo.py`** — Compares the same checkout flow across REST, MCP, and A2A transports.
Uses UCP SDK Pydantic models and writes to BigQuery.

```bash
uv run python examples/transport_demo.py
```

Uses `classify_jsonrpc()` for MCP/A2A tool name mapping. Produces identical
event types with different `transport` values (`rest`, `mcp`, `a2a`).

**Event types:** `profile_discovered`, `checkout_session_created`,
`checkout_session_updated`, `checkout_session_completed`, `capability_negotiated`

---

## Identity & Payment Demo

**`identity_payment_demo.py`** — Identity linking and payment negotiation flows.
Uses UCP SDK Pydantic models and writes to BigQuery.

```bash
uv run python examples/identity_payment_demo.py
```

**Flows:**

1. **OAuth identity linking** — initiate -> callback -> linked
2. **Identity revocation** — revoke an existing link
3. **Payment handler negotiation** — compute intersection of platform + merchant handlers
4. **Payment instrument selection** — buyer picks from available instruments
5. **Payment failure + success** — `fail_token` -> payment_failed, `success_token` -> payment_completed

**Event types:** `identity_link_initiated`, `identity_link_completed`,
`identity_link_revoked`, `payment_handler_negotiated`,
`payment_instrument_selected`, `payment_completed`, `payment_failed`

---

## E2E Demo (Local SQLite)

**`e2e_demo.py`** — A fully self-contained demo that requires no GCP
credentials. Uses SQLite instead of BigQuery so you can try UCP Analytics
in seconds.

```bash
pip install fastapi uvicorn httpx
python examples/e2e_demo.py
```

Runs the same checkout flow (discovery, create, update, discount, complete,
ship) and prints a local analytics report with funnel, financials, payment,
capabilities, and latency stats.

---

## Cleanup

To delete the demo data from BigQuery after testing:

```sql
-- Delete only demo rows (preserves production data)
DELETE FROM `YOUR_PROJECT.ucp_analytics.ucp_events`
WHERE app_name IN ('bq_demo', 'bq_adk_demo', 'scenarios_demo',
                   'cart_demo', 'order_lifecycle_demo',
                   'transport_demo', 'identity_payment_demo');
```

Or drop the entire table:

```sql
DROP TABLE IF EXISTS `YOUR_PROJECT.ucp_analytics.ucp_events`;
```
