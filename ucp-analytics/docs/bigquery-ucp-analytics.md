# BigQuery Commerce Analytics for UCP

## Overview

This plugin provides **structured commerce observability** for agents and merchants
using the [Universal Commerce Protocol (UCP)](https://ucp.dev). It captures checkout
sessions, cart operations, order lifecycle, payment flows, capability negotiation,
and identity linking events into Google BigQuery for funnel analysis, error debugging,
latency monitoring, and revenue attribution.

Three integration points — pick any or combine:

| Integration | Side | How |
|---|---|---|
| **FastAPI middleware** | Merchant server | Intercepts inbound UCP HTTP traffic |
| **HTTPX event hook** | Agent / platform client | Intercepts outbound UCP HTTP calls |
| **ADK plugin** | Google ADK agent | Wraps tool callbacks into UCP events |

All three route events through the same `UCPAnalyticsTracker` → `AsyncBigQueryWriter`
pipeline, which batches rows and streams them into a partitioned, clustered BigQuery table.

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

> **Spec alignment:** All field names, total types, payment structures, and metadata
> envelopes follow the [official UCP specification](https://github.com/Universal-Commerce-Protocol/ucp).

---

## Key Use Cases

- **Checkout funnel analysis** — Measure conversion from discovery → cart → checkout → completion
- **Revenue attribution** — Track GMV by merchant, payment handler, fulfillment type, and geography
- **Latency monitoring** — Percentile breakdowns (p50/p95/p99) per operation type
- **Error debugging** — Surface escalations, failed payments, and server error messages
- **Capability adoption** — Track which UCP capabilities and extensions merchants support
- **Discount effectiveness** — Analyze discount code usage and applied discount allocations
- **Agent performance** — Compare checkout success rates across ADK agents

---

## Prerequisites

| Requirement | Details |
|---|---|
| **GCP Project** | BigQuery API enabled |
| **Auth (local)** | `gcloud auth application-default login` |
| **Python** | 3.10+ |
| **Source** | Clone the [Data Agent Kit](https://github.com/haiyuan-eng-google/data-agent-kit) repository (see Installation below) |

### Enable BigQuery API

```bash
gcloud services enable bigquery.googleapis.com
```

### Authenticate

```bash
gcloud auth application-default login
```

### Required IAM Roles

| Role | Scope | Purpose |
|---|---|---|
| `roles/bigquery.jobUser` | Project | Run queries and streaming inserts |
| `roles/bigquery.dataEditor` | Dataset | Write event rows |

> **Note:** If `auto_create_table=True` (the default), the service account also needs
> `roles/bigquery.dataOwner` at the dataset level to create the table and dataset on
> first write.

---

## Installation

Install from source by cloning the [Data Agent Kit](https://github.com/haiyuan-eng-google/data-agent-kit)
repository and using the `ucp-analytics/` folder directly:

```bash
git clone https://github.com/haiyuan-eng-google/data-agent-kit.git
cd data-agent-kit/ucp-analytics
```

Then install with pip in editable mode (so changes to the source tree are
picked up without reinstalling):

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

---

## Usage

### Integration 1: FastAPI Middleware (Merchant Server)

Add two lines to your UCP merchant server to capture every inbound checkout,
cart, order, and discovery request:

```python
from fastapi import FastAPI
from ucp_analytics import UCPAnalyticsTracker, UCPAnalyticsMiddleware

app = FastAPI()

tracker = UCPAnalyticsTracker(
    project_id="my-gcp-project",
    app_name="flower_shop",
    # Optional: widen webhook detection if the platform publishes a
    # non-default URL (UCP `order.md` says the URL is platform-specific).
    webhook_path_prefixes=["/events"],
    # Optional: derive signature alg from JWK crv per UCP signatures.md.
    # The callable returns the JWK dict for a given keyid.
    jwk_lookup=my_jwks_cache.get,
)
app.add_middleware(UCPAnalyticsMiddleware, tracker=tracker)

@app.on_event("shutdown")
async def shutdown():
    await tracker.close()  # drains in-flight tasks, then flushes
```

**How it works:**

1. The middleware checks if the request path matches a UCP operation
   (`/checkout-sessions`, `/carts`, `/.well-known/ucp`, `/orders`, `/identity`,
   `/oauth2`, `/webhooks`, the OAuth/OIDC discovery endpoints), OR if the
   request carries the Standard Webhooks header pair (`Webhook-Id` +
   `Webhook-Timestamp`) for header-based webhook detection on
   platform-specific URLs. Operator-configured `webhook_path_prefixes` on
   the tracker (e.g. `/events`, `/ucp-events`, `/hooks`) widen this set.
2. Reads the request body (for POST/PUT/PATCH)
3. Lets the handler execute normally and measures latency
4. Reads the response body
5. Passes both to `UCPAnalyticsTracker.record_http()` as a fire-and-forget
   task, registered on the tracker so `tracker.close()` drains in-flight
   tasks before flushing. Multi-line `WWW-Authenticate` headers are merged
   (RFC 7235 §4.1) so Bearer challenges survive `dict()` collapse.
   - For webhook paths, the tracker uses the **request body** (order payload)
     for classification and field extraction, since the response is just an ack
6. Non-UCP paths pass through with zero overhead

> **Requires:** the `[fastapi]` extra (`pip install -e ".[fastapi]"` from
> `data-agent-kit/ucp-analytics/`).

### Integration 2: HTTPX Client Hook (Agent / Platform)

Instrument your shopping agent's HTTP client to capture every outbound UCP call:

```python
import httpx
from ucp_analytics import UCPAnalyticsTracker, UCPClientEventHook

tracker = UCPAnalyticsTracker(
    project_id="my-gcp-project",
    app_name="shopping_agent",
)
hook = UCPClientEventHook(tracker)

async with httpx.AsyncClient(
    event_hooks={"response": [hook]},
) as client:
    # Every UCP call is automatically tracked
    resp = await client.get("https://merchant.example.com/.well-known/ucp")
    resp = await client.post(
        "https://merchant.example.com/checkout-sessions",
        json={"line_items": [{"item_id": "roses", "quantity": 1}]},
    )

await tracker.close()
```

**How it works:**

1. The hook fires after every HTTP response
2. Checks if the request path contains a UCP pattern
3. Reads both request and response bodies
4. Records latency from `response.elapsed`
5. Passes everything to `UCPAnalyticsTracker.record_http()`

### Integration 3: ADK Plugin (Google ADK Agent)

For agents built with the [Google Agent Development Kit](https://google.github.io/adk-docs/),
the `UCPAgentAnalyticsPlugin` wraps UCP analytics into ADK's `BasePlugin` interface:

```python
from google.adk.runners import InMemoryRunner
from ucp_analytics.adk_plugin import UCPAgentAnalyticsPlugin

plugin = UCPAgentAnalyticsPlugin(
    project_id="my-gcp-project",
    dataset_id="ucp_analytics",
    app_name="adk_shopping_agent",
    batch_size=1,         # flush every event for demos
    track_all_tools=False, # only record UCP tools
)

runner = InMemoryRunner(agent=my_agent, plugins=[plugin])

# ... run your agent ...

await plugin.close()
```

**How it works:**

1. `before_tool_callback` records a start timestamp
2. `after_tool_callback` fires after the tool returns
3. The plugin checks if the tool name matches a UCP pattern (e.g., `create_checkout`, `discover_merchant`)
4. Maps the tool name to an equivalent HTTP operation via `_TOOL_TO_HTTP` lookup table
5. Classifies the event type using the same `UCPResponseParser` as HTTP integrations
6. Extracts structured fields from the tool result
7. Computes latency from the before/after timing gap
8. Writes the event to BigQuery
9. Non-UCP tools (e.g., `get_weather`) are silently skipped

> **Requires:** the `[adk]` extra (`pip install -e ".[adk]"` from
> `data-agent-kit/ucp-analytics/`).

#### ADK Tool Name Mapping

The plugin maps ADK tool names to equivalent UCP HTTP operations for accurate
event classification:

| Tool Name | HTTP Equivalent | Event Type |
|---|---|---|
| `discover_merchant` | `GET /.well-known/ucp` | `profile_discovered` |
| `create_checkout` | `POST /checkout-sessions` | `checkout_session_created` |
| `update_checkout` | `PUT /checkout-sessions/{id}` | `checkout_session_updated` |
| `add_to_checkout` | `PUT /checkout-sessions/{id}` | `checkout_session_updated` |
| `remove_from_checkout` | `PUT /checkout-sessions/{id}` | `checkout_session_updated` |
| `update_customer_details` | `PUT /checkout-sessions/{id}` | `checkout_session_updated` |
| `start_payment` | `PUT /checkout-sessions/{id}` | `checkout_session_updated` |
| `complete_checkout` | `POST /checkout-sessions/{id}/complete` | `checkout_session_completed` |
| `cancel_checkout` | `POST /checkout-sessions/{id}/cancel` | `checkout_session_canceled` |
| `create_cart` | `POST /carts` | `cart_created` |
| `update_cart` | `PUT /carts/{id}` | `cart_updated` |
| `cancel_cart` | `POST /carts/{id}/cancel` | `cart_canceled` |
| `create_order` | `POST /orders` | `order_created` |
| `get_weather` | *(not a UCP tool)* | *(skipped)* |

> **Note:** `start_payment` maps to a checkout **update** (not completion) because it is a
> pre-completion step that presents payment options. The actual finalization is `complete_checkout`.

Tools not in the mapping are skipped by default. Set `track_all_tools=True` to
record them as generic `request` events.

### Integration 4: Direct API

For custom integrations, call the tracker directly:

```python
from ucp_analytics import UCPAnalyticsTracker

tracker = UCPAnalyticsTracker(project_id="my-gcp-project")

event = await tracker.record_http(
    method="POST",
    path="/checkout-sessions",
    status_code=201,
    response_body={
        "id": "chk_abc123",
        "status": "incomplete",
        "currency": "USD",
        "totals": [
            {"type": "subtotal", "amount": 2999},
            {"type": "total", "amount": 2999},
        ],
    },
    latency_ms=42.5,
)

await tracker.close()
```

Or construct events manually:

```python
from ucp_analytics import UCPAnalyticsTracker, UCPEvent

tracker = UCPAnalyticsTracker(project_id="my-gcp-project")

event = UCPEvent(
    event_type="checkout_session_created",
    app_name="my_app",
    checkout_session_id="chk_abc123",
    checkout_status="incomplete",
    currency="USD",
    total_amount=2999,
    latency_ms=42.5,
)
await tracker.record_event(event)

await tracker.close()
```

---

## Configuration Options

### `UCPAnalyticsTracker`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `project_id` | `str` | *(required)* | Google Cloud project ID |
| `dataset_id` | `str` | `"ucp_analytics"` | BigQuery dataset name |
| `table_id` | `str` | `"ucp_events"` | BigQuery table name |
| `app_name` | `str` | `""` | Application name tag on every event |
| `batch_size` | `int` | `50` | Flush to BigQuery every N events |
| `auto_create_table` | `bool` | `True` | Create dataset + table on first write |
| `redact_pii` | `bool` | `False` | Recursively redact PII fields in bodies before extraction |
| `pii_fields` | `list[str]` | `["email", "phone", ...]` | **Override** (not extend) the redaction set. Passing a list replaces the defaults (`email`, `phone`, `first_name`, `last_name`, `phone_number`, `street_address`, `postal_code`) wholesale — include them in your own list to keep them. AP2 credential names (`merchant_authorization`, `checkout_mandate`) and documented PII signal keys (`dev.ucp.buyer_ip`, `dev.ucp.user_agent`) are always force-included afterward regardless of operator config. |
| `custom_metadata` | `dict[str, str]` | `None` | Static key-value pairs added as JSON to every event |
| `webhook_path_prefixes` | `list[str]` | `None` | Additional path prefixes for order webhooks (per UCP `order.md`: *"The URL format is platform-specific"*). The Standard Webhooks header pair (`Webhook-Id` + `Webhook-Timestamp`) also triggers detection on unknown paths, suppressed on known UCP REST paths. |
| `jwk_lookup` | `Callable[[str], Optional[Mapping]]` | `None` | Operator-provided keyid → JWK lookup. Used to derive `request_signature_alg` / `response_signature_alg` from the JWK's `crv` per UCP `signatures.md`. When absent, the alg columns stay NULL. |
| `include_ap2_raw` | `bool` | `False` | Surface raw `body.ap2` into `ap2_mandate_raw_json` after `_redact`. Credential field names are scrubbed regardless of `redact_pii`. Default-off forensic capture. |
| `include_signals_raw` | `bool` | `False` | Surface raw `body.signals` into `signals_json` after `_redact`. Documented PII signal keys scrubbed regardless of `redact_pii`. Default-off forensic capture. |

### `AsyncBigQueryWriter`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `project_id` | `str` | *(required)* | Google Cloud project ID |
| `dataset_id` | `str` | *(required)* | BigQuery dataset name |
| `table_id` | `str` | `"ucp_events"` | BigQuery table name |
| `batch_size` | `int` | `50` | Events per write batch |
| `auto_create_table` | `bool` | `True` | Auto-create table on first write |
| `max_buffer_size` | `int` | `10000` | In-memory buffer cap; oldest events dropped when full |

### `UCPAgentAnalyticsPlugin` (ADK)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `project_id` | `str` | *(required)* | Google Cloud project ID |
| `dataset_id` | `str` | `"ucp_analytics"` | BigQuery dataset name |
| `table_id` | `str` | `"ucp_events"` | BigQuery table name |
| `app_name` | `str` | `""` | Application name tag |
| `batch_size` | `int` | `50` | Flush every N events |
| `track_all_tools` | `bool` | `False` | Record non-UCP tools as generic `request` events |
| `redact_pii` | `bool` | `False` | Redact PII fields |
| `custom_metadata` | `dict[str, str]` | `None` | Static metadata on every event |

---

## BigQuery Schema Reference (`ucp_events`)

The table is automatically created with daily partitioning on `timestamp` and
clustering on `event_type`, `checkout_session_id`, `merchant_host`.

### Identity

| Field | Type | Mode | Description |
|---|---|---|---|
| `event_id` | `STRING` | `REQUIRED` | Unique UUID per event |
| `event_type` | `STRING` | `REQUIRED` | Classified event type (see [Event Types](#event-types)) |
| `timestamp` | `TIMESTAMP` | `REQUIRED` | UTC event time (ISO 8601) |

### Context

| Field | Type | Mode | Description |
|---|---|---|---|
| `app_name` | `STRING` | `NULLABLE` | Application identifier (e.g., `"flower_shop"`) |
| `merchant_host` | `STRING` | `NULLABLE` | Merchant endpoint hostname |
| `platform_profile_url` | `STRING` | `NULLABLE` | Raw `UCP-Agent` request header (legacy; superseded by `ucp_agent_profile_url`) |
| `ucp_agent_profile_url` | `STRING` | `NULLABLE` | `profile` member parsed out of the RFC 8941 `UCP-Agent` Structured Field Dictionary; direction-neutral |
| `transport` | `STRING` | `NULLABLE` | Transport protocol: `rest`, `mcp`, `a2a`, `embedded` |

### HTTP

| Field | Type | Mode | Description |
|---|---|---|---|
| `http_method` | `STRING` | `NULLABLE` | HTTP method (`GET`, `POST`, `PUT`) |
| `http_path` | `STRING` | `NULLABLE` | Request path (e.g., `/checkout-sessions`) |
| `http_status_code` | `INTEGER` | `NULLABLE` | HTTP response status code |
| `idempotency_key` | `STRING` | `NULLABLE` | `Idempotency-Key` request header |
| `request_id` | `STRING` | `NULLABLE` | `Request-Id` request header |

### Request-Body Context

Captured from `body.context` on requests carrying a UCP Context object (e.g. checkout-create, catalog-search). Survives the request/response merge.

| Field | Type | Mode | Description |
|---|---|---|---|
| `context_intent` | `STRING` | `NULLABLE` | Buyer intent / agent task |
| `context_language` | `STRING` | `NULLABLE` | BCP 47 language tag |
| `context_currency` | `STRING` | `NULLABLE` | ISO 4217 currency code |
| `context_eligibility_json` | `JSON` | `NULLABLE` | Eligibility claim payload |

### Checkout

| Field | Type | Mode | Description |
|---|---|---|---|
| `checkout_session_id` | `STRING` | `NULLABLE` | Checkout session identifier |
| `checkout_status` | `STRING` | `NULLABLE` | Session state: `incomplete`, `requires_escalation`, `ready_for_complete`, `complete_in_progress`, `completed`, `canceled`. Only populated for checkout responses (not orders or carts). |
| `order_id` | `STRING` | `NULLABLE` | Order ID (from `checkout.order.id` or direct) |
| `order_label` | `STRING` | `NULLABLE` | Optional business-set order label per `order.json` (surfaced only on order-shaped bodies carrying `checkout_id`) |

### Financial (Minor Units)

All amounts are in **minor currency units** (cents for USD), signed per `signed_amount.json` (negative for refunds). The seven well-known total types from the UCP spec map to scalar columns; each is `SUM(amount)` over all entries of the matching type (so split state+local tax rows or multi-line discounts accumulate correctly). Business-defined types (per `total.json`'s open vocabulary) are preserved verbatim in `totals_json`.

| Field | Type | Mode | Description |
|---|---|---|---|
| `currency` | `STRING` | `NULLABLE` | ISO 4217 currency code (e.g., `USD`) |
| `items_discount_amount` | `INTEGER` | `NULLABLE` | `SUM(totals[type=items_discount].amount)` |
| `subtotal_amount` | `INTEGER` | `NULLABLE` | `SUM(totals[type=subtotal].amount)` |
| `discount_amount` | `INTEGER` | `NULLABLE` | `SUM(totals[type=discount].amount)` |
| `fulfillment_amount` | `INTEGER` | `NULLABLE` | `SUM(totals[type=fulfillment].amount)` |
| `tax_amount` | `INTEGER` | `NULLABLE` | `SUM(totals[type=tax].amount)` |
| `fee_amount` | `INTEGER` | `NULLABLE` | `SUM(totals[type=fee].amount)` |
| `total_amount` | `INTEGER` | `NULLABLE` | `SUM(totals[type=total].amount)` |
| `totals_json` | `JSON` | `NULLABLE` | Full ordered totals array verbatim (duplicates, `display_text`, `lines[]`, business-defined types) |

### Line Items

| Field | Type | Mode | Description |
|---|---|---|---|
| `line_items_json` | `JSON` | `NULLABLE` | Full line items array |
| `line_item_count` | `INTEGER` | `NULLABLE` | Number of line items |

### Payment

| Field | Type | Mode | Description |
|---|---|---|---|
| `payment_handler_id` | `STRING` | `NULLABLE` | Payment handler reverse-domain ID (e.g., `com.stripe.payment`) |
| `payment_instrument_type` | `STRING` | `NULLABLE` | Instrument type: `card`, `bank_transfer`, etc. |
| `payment_brand` | `STRING` | `NULLABLE` | Card brand: `Visa`, `Mastercard`, etc. |
| `payment_available_instruments_json` | `JSON` | `NULLABLE` | Per-handler `available_instruments[]` from `body.ucp.payment_handlers[*]` (all handlers preserved with per-handler arrays intact) |

### Capabilities

| Field | Type | Mode | Description |
|---|---|---|---|
| `ucp_version` | `STRING` | `NULLABLE` | UCP protocol version (e.g., `2026-01-11`) |
| `capabilities_json` | `JSON` | `NULLABLE` | Capabilities array from `ucp.capabilities` (per SDK: array of `{name, version}` objects) |
| `extensions_json` | `JSON` | `NULLABLE` | Extensions metadata |

### Identity Linking

| Field | Type | Mode | Description |
|---|---|---|---|
| `identity_provider` | `STRING` | `NULLABLE` | OAuth identity provider |
| `identity_scope` | `STRING` | `NULLABLE` | Requested OAuth scope |

### Fulfillment

| Field | Type | Mode | Description |
|---|---|---|---|
| `fulfillment_type` | `STRING` | `NULLABLE` | `shipping`, `pickup`, `digital`, `service` |
| `fulfillment_destination_country` | `STRING` | `NULLABLE` | ISO 3166-1 alpha-2 country code |

### Discount Extension

| Field | Type | Mode | Description |
|---|---|---|---|
| `discount_codes_json` | `JSON` | `NULLABLE` | Input discount codes (from `discounts.codes`) |
| `discount_applied_json` | `JSON` | `NULLABLE` | Applied discounts with allocations (from `discounts.applied`) |

### Checkout Metadata

| Field | Type | Mode | Description |
|---|---|---|---|
| `expires_at` | `TIMESTAMP` | `NULLABLE` | Checkout session expiration time |
| `continue_url` | `STRING` | `NULLABLE` | URL for escalation / human handoff |

### Order

| Field | Type | Mode | Description |
|---|---|---|---|
| `permalink_url` | `STRING` | `NULLABLE` | Order status page URL (from `order.permalink_url`) |

### Order Lifecycle (c5c6139 schema)

At UCP `c5c6139`, `order.json` no longer carries a top-level `status` — lifecycle lives in two append-only arrays. `latest_*` scalars are picked by RFC 3339-parsed `occurred_at` (mixed `Z` / `±HH:MM` offsets compared as instants; naive timestamps rejected).

| Field | Type | Mode | Description |
|---|---|---|---|
| `fulfillment_events_json` | `JSON` | `NULLABLE` | Full ordered `fulfillment.events[]` (shipment log) |
| `adjustments_json` | `JSON` | `NULLABLE` | Full ordered `adjustments[]` (refunds / returns / disputes / cancellations) |
| `latest_fulfillment_event_type` | `STRING` | `NULLABLE` | Type of the latest fulfillment event (`processing`, `shipped`, `in_transit`, `delivered`, `failed_attempt`, `canceled`, `undeliverable`, `returned_to_sender`) |
| `latest_fulfillment_event_at` | `TIMESTAMP` | `NULLABLE` | `occurred_at` of the latest fulfillment event |
| `latest_adjustment_type` | `STRING` | `NULLABLE` | Type of the latest adjustment (`refund`, `return`, `cancellation`, etc.) |
| `latest_adjustment_status` | `STRING` | `NULLABLE` | `pending` / `completed` / `failed` |
| `latest_adjustment_at` | `TIMESTAMP` | `NULLABLE` | `occurred_at` of the latest adjustment |

### HTTP Message Signing (RFC 9421 + UCP `signatures.md`)

The BOOL columns are three-state: NULL means "headers never observed" (direct API caller didn't pass the corresponding side), FALSE means "observed and unsigned", TRUE means "complete Signature-Input + Signature pair present".

| Field | Type | Mode | Description |
|---|---|---|---|
| `request_signed` | `BOOL` | `NULLABLE` | Both `Signature-Input` and `Signature` present on request |
| `response_signed` | `BOOL` | `NULLABLE` | Same on response side |
| `request_signature_keyid` | `STRING` | `NULLABLE` | First `keyid` parsed from `Signature-Input` (captured even on half-signed for forensics) |
| `response_signature_keyid` | `STRING` | `NULLABLE` | Same on response side |
| `request_signature_alg` | `STRING` | `NULLABLE` | JWA name (`ES256` / `ES384`) derived from JWK `crv` via `jwk_lookup`; populated only when `request_signed=True` |
| `response_signature_alg` | `STRING` | `NULLABLE` | Same on response side |

### Webhook Metadata (Standard Webhooks)

Scoped to webhook flows only — stamping these headers on a non-webhook UCP REST request is rejected.

| Field | Type | Mode | Description |
|---|---|---|---|
| `webhook_id` | `STRING` | `NULLABLE` | `Webhook-Id` request header (unique event ID) |
| `webhook_timestamp` | `TIMESTAMP` | `NULLABLE` | `Webhook-Timestamp` parsed from Unix seconds → ISO 8601 UTC |

### WWW-Authenticate Bearer Challenge (RFC 7235 / 6750 / 9728)

Surfaced on response side; parsed RFC-faithfully across multi-challenge / multi-line / BWS-around-`=` / token-form-value / hyphenated-scheme cases.

| Field | Type | Mode | Description |
|---|---|---|---|
| `auth_challenge_realm` | `STRING` | `NULLABLE` | Bearer challenge `realm` |
| `auth_challenge_error` | `STRING` | `NULLABLE` | Bearer challenge `error` (`invalid_token` / `insufficient_scope` / ...) |
| `auth_challenge_scope` | `STRING` | `NULLABLE` | Bearer challenge `scope` |
| `auth_challenge_resource_metadata` | `STRING` | `NULLABLE` | RFC 9728 `resource_metadata` pointer |

### Embedded Checkout (server-observable slice)

Runtime postMessage events (`ec.totals.change`, link delegation acceptance, reauth, cart binding) are **deferred** — they live in the iframe / host browser and need separate instrumentation that doesn't exist in this library.

| Field | Type | Mode | Description |
|---|---|---|---|
| `embedded_delegations_json` | `JSON` | `NULLABLE` | Union of `delegate[]` across all embedded services in `/.well-known/ucp` discovery responses |
| `embedded_color_schemes_json` | `JSON` | `NULLABLE` | Union of `color_scheme[]` across all embedded services |
| `embedded_ec_color_scheme` | `STRING` | `NULLABLE` | `ec_color_scheme` URL query parameter (parsed from `url` or `path`) |

### AP2 Mandates + Buyer Consent

Safe-by-default columns observe non-PII metadata only. The raw column is opt-in via `include_ap2_raw=True`; credential field names are force-included in `pii_fields` so values are always scrubbed before serialization.

| Field | Type | Mode | Description |
|---|---|---|---|
| `ap2_mandate_present` | `BOOL` | `NULLABLE` | Whether `body.ap2` carries any mandate field |
| `ap2_mandate_keys_json` | `JSON` | `NULLABLE` | Names of present mandate fields (`merchant_authorization` / `checkout_mandate`) |
| `ap2_mandate_metadata_json` | `JSON` | `NULLABLE` | Per-mandate JOSE header (`kid` / `alg` / `typ`) + SHA-256 hex of the credential string. **NEVER** the payload (credential body) or disclosures. |
| `buyer_consent_json` | `JSON` | `NULLABLE` | Whitelisted boolean consent flags from `body.buyer.consent` only (`analytics` / `preferences` / `marketing` / `sale_of_data`). **NEVER** the parent buyer object's PII. |
| `ap2_mandate_raw_json` | `JSON` | `NULLABLE` | (Opt-in) Original `body.ap2` after `_redact`. Credential strings always scrubbed. |

### Authorization Signals

Safe-by-default columns capture only key names. The raw column is opt-in via `include_signals_raw=True`; documented PII signal keys (`dev.ucp.buyer_ip`, `dev.ucp.user_agent`) force-included in `pii_fields`.

| Field | Type | Mode | Description |
|---|---|---|---|
| `signals_present` | `BOOL` | `NULLABLE` | Whether `body.signals` carries any entries |
| `signals_keys_json` | `JSON` | `NULLABLE` | Names of signal keys (reverse-domain identifiers); **NEVER** the values |
| `signals_json` | `JSON` | `NULLABLE` | (Opt-in) Original `body.signals` after `_redact`. Documented PII signal values always scrubbed. Operators can provide a custom `pii_fields` set (override semantics — include the defaults yourself if you want to preserve them) for additional reverse-domain PII signals; the force-included keys are always OR'd in afterward. |

### Errors & Messages

| Field | Type | Mode | Description |
|---|---|---|---|
| `error_code` | `STRING` | `NULLABLE` | First error's `code` from `messages[]` |
| `error_message` | `STRING` | `NULLABLE` | First error's `content` |
| `error_severity` | `STRING` | `NULLABLE` | First error's `severity` (`recoverable`/`escalation`/`fatal`) |
| `messages_json` | `JSON` | `NULLABLE` | Full `messages[]` array verbatim |
| `message_info_codes_json` | `JSON` | `NULLABLE` | Dedup'd, order-preserved list of info-severity codes |
| `message_warning_codes_json` | `JSON` | `NULLABLE` | Dedup'd, order-preserved list of warning-severity codes |
| `identity_optional_present` | `BOOL` | `NULLABLE` | Three-state: TRUE iff `identity_optional` is in the info codes, FALSE iff info codes observed but it's not, NULL iff no info codes |
| `eligibility_accepted_present` | `BOOL` | `NULLABLE` | Three-state eligibility outcome — denominator is "any eligibility outcome code observed" (cross-severity capture) |
| `eligibility_not_accepted_present` | `BOOL` | `NULLABLE` | Same denominator |
| `eligibility_invalid_present` | `BOOL` | `NULLABLE` | Same denominator |

### Performance

| Field | Type | Mode | Description |
|---|---|---|---|
| `latency_ms` | `FLOAT` | `NULLABLE` | End-to-end request latency in milliseconds |

### Custom

| Field | Type | Mode | Description |
|---|---|---|---|
| `custom_metadata_json` | `JSON` | `NULLABLE` | User-defined key-value metadata |

---

## Event Types

Events are auto-classified from the HTTP method, path, and response status. The
classifier handles all UCP resource types:

### Checkout Events

| Event Type | Trigger | Description |
|---|---|---|
| `checkout_session_created` | `POST /checkout-sessions` | New checkout session started |
| `checkout_session_get` | `GET /checkout-sessions/{id}` | Checkout session retrieved |
| `checkout_session_updated` | `PUT /checkout-sessions/{id}` | Buyer info, fulfillment, or items updated |
| `checkout_escalation` | `PUT /checkout-sessions/{id}` (status=`requires_escalation`) | Agent cannot proceed; human handoff needed |
| `checkout_session_completed` | `POST /checkout-sessions/{id}/complete` | Checkout completed, order placed |
| `checkout_session_canceled` | `POST /checkout-sessions/{id}/cancel` | Checkout abandoned or canceled |

### Cart Events

| Event Type | Trigger | Description |
|---|---|---|
| `cart_created` | `POST /carts` | New cart created |
| `cart_get` | `GET /carts/{id}` | Cart retrieved |
| `cart_updated` | `PUT /carts/{id}` | Cart items or metadata updated |
| `cart_canceled` | `POST /carts/{id}/cancel` | Cart abandoned or canceled |

### Catalog Events

| Event Type | Trigger | Description |
|---|---|---|
| `catalog_search` | `POST /catalog/search` | Catalog search query |
| `catalog_lookup` | `POST /catalog/lookup` | Catalog item lookup by ID |
| `catalog_product_get` | `POST /catalog/product` | Single product detail fetch |

### Order Events

| Event Type | Trigger | Description |
|---|---|---|
| `order_created` | `POST /orders` | Order created |
| `order_get` | `GET /orders/{id}` (no lifecycle in body) | Read-only poll of order state; distinct from `order_updated` |
| `order_updated` | `PUT /orders/{id}` (no lifecycle in body) | REST-driven order mutation |
| `order_shipped` | Latest `fulfillment.events[].type` is `shipped` / `in_transit` | Shipment in motion |
| `order_delivered` | Latest `fulfillment.events[].type` is `delivered` | Delivery confirmed |
| `order_returned` | Latest `fulfillment.events[].type` is `returned_to_sender` or `adjustments[].type` is `refund`/`return` | Return processed |
| `order_canceled` | Latest `fulfillment.events[].type` is `canceled`/`undeliverable` or `adjustments[].type` is `cancellation` | Order canceled |
| `order_webhook_received` | Webhook delivery without recognizable lifecycle status | Distinct from `order_updated` (REST PUT mutation); preserves webhook-vs-REST taxonomy |

**Lifecycle derivation (c5c6139):** At UCP `c5c6139` the order schema dropped its top-level `status`. Lifecycle derives from `fulfillment.events[]` (latest by RFC 3339 `occurred_at`) → `adjustments[]` → legacy top-level `status` for pre-c5c6139 senders. Lifecycle wins on either GET or PUT — a GET that returns a `delivered` fulfillment event still classifies as `order_delivered`.

**Webhook detection:** Two signals — path matches `/webhook(s)` (default) or operator-configured `webhook_path_prefixes`, OR Standard Webhooks header pair (`Webhook-Id` + `Webhook-Timestamp`) on an unknown URL. Header-based detection is suppressed on known UCP REST paths so stamped headers can't override URL semantics. Webhook 4xx/5xx responses classify as `error`. Legacy URL-segment fallbacks (`/webhooks/order-delivered` etc.) retained for back-compat with senders that don't include status in body — body-driven derivation takes precedence.

### Discovery & Capability Events

| Event Type | Trigger | Description |
|---|---|---|
| `profile_discovered` | `GET /.well-known/ucp` | Merchant UCP profile fetched |
| `capability_negotiated` | Capability exchange / A2A negotiation | Capabilities agreed upon |

### Identity Events

| Event Type | Trigger | Description |
|---|---|---|
| `identity_link_initiated` | `POST /identity`, `/oauth2/authorize`, or any of the OAuth/OIDC metadata discovery endpoints (`/.well-known/oauth-authorization-server`, `/.well-known/openid-configuration`, `/.well-known/oauth-protected-resource`) | Identity linking started |
| `identity_link_completed` | `GET /identity/callback`, `/oauth2/token` | Identity linked via OAuth callback / token exchange |
| `identity_link_revoked` | `POST /identity/revoke`, `/oauth2/revoke`, `DELETE /identity/*` | Identity link removed |

### Payment Events

| Event Type | Trigger | Description |
|---|---|---|
| `payment_handler_negotiated` | Handler selection | Payment handler agreed upon |
| `payment_instrument_selected` | Instrument selection | Buyer selects payment instrument |
| `payment_completed` | Successful payment | Payment processed |
| `payment_failed` | Failed payment | Payment declined or errored |

### Fallback Events

| Event Type | Trigger | Description |
|---|---|---|
| `request` | Unmatched UCP path | Generic request (no specific classification) |
| `error` | HTTP status >= 400 | Server or client error |

---

## Event Payload Example

A `checkout_session_completed` event row in BigQuery:

```json
{
  "event_id": "670cf848-070c-4a2b-b8e1-2c4f1e8d3a5b",
  "event_type": "checkout_session_completed",
  "timestamp": "2026-05-12T10:30:00.000Z",
  "app_name": "flower_shop",
  "merchant_host": "flower-shop.example.com",
  "transport": "rest",
  "ucp_agent_profile_url": "https://platform.example/profile",
  "http_method": "POST",
  "http_path": "/checkout-sessions/chk_abc123/complete",
  "http_status_code": 200,
  "checkout_session_id": "chk_abc123",
  "checkout_status": "completed",
  "order_id": "order_xyz789",
  "order_label": "ORD-2026-00042",
  "currency": "USD",
  "subtotal_amount": 7997,
  "fulfillment_amount": 599,
  "tax_amount": 700,
  "total_amount": 8796,
  "discount_amount": 500,
  "totals_json": "[{\"type\":\"subtotal\",\"amount\":7997},{\"type\":\"tax\",\"amount\":500,\"display_text\":\"state tax\"},{\"type\":\"tax\",\"amount\":200,\"display_text\":\"local tax\"},{\"type\":\"fulfillment\",\"amount\":599},{\"type\":\"discount\",\"amount\":-500},{\"type\":\"total\",\"amount\":8796}]",
  "line_item_count": 3,
  "payment_handler_id": "com.stripe.payment",
  "payment_instrument_type": "card",
  "payment_brand": "Visa",
  "ucp_version": "2026-05-06",
  "fulfillment_type": "shipping",
  "fulfillment_destination_country": "US",
  "discount_codes_json": "[\"FLOWERS10\"]",
  "permalink_url": "https://flower-shop.example.com/orders/order_xyz789",
  "request_signed": true,
  "response_signed": true,
  "request_signature_keyid": "platform-key-2026",
  "response_signature_keyid": "merchant-key-2026",
  "request_signature_alg": "ES256",
  "response_signature_alg": "ES256",
  "context_intent": "buy a birthday gift",
  "context_language": "en-US",
  "context_currency": "USD",
  "identity_optional_present": false,
  "latency_ms": 142.5
}
```

A complementary `order_delivered` event from a webhook delivery looks like:

```json
{
  "event_id": "8c1a3...",
  "event_type": "order_delivered",
  "timestamp": "2026-05-12T17:30:00.000Z",
  "app_name": "flower_shop",
  "http_method": "POST",
  "http_path": "/hooks/abc-123",
  "http_status_code": 200,
  "webhook_id": "evt_42",
  "webhook_timestamp": "2026-05-12T17:30:00+00:00",
  "order_id": "order_xyz789",
  "latest_fulfillment_event_type": "delivered",
  "latest_fulfillment_event_at": "2026-05-12T17:00:00+00:00",
  "fulfillment_events_json": "[{\"id\":\"fe_1\",\"occurred_at\":\"2026-05-10T08:00:00Z\",\"type\":\"shipped\",\"tracking_number\":\"1Z999\"},{\"id\":\"fe_2\",\"occurred_at\":\"2026-05-12T17:00:00+00:00\",\"type\":\"delivered\"}]",
  "request_signed": true,
  "request_signature_keyid": "platform-key-2026",
  "request_signature_alg": "ES256"
}
```

---

## PII Redaction

Enable PII redaction to mask sensitive fields before they reach BigQuery:

```python
tracker = UCPAnalyticsTracker(
    project_id="my-gcp-project",
    redact_pii=True,
    pii_fields=["email", "phone", "first_name", "last_name",
                "phone_number", "street_address", "postal_code"],
)
```

When enabled, any matching field (case-insensitive on key name) in the request
or response body is replaced with `"[REDACTED]"` before extraction. This applies
recursively to nested objects and arrays. Non-string dict keys (int / None /
tuple) are handled gracefully — they can't match `pii_fields` but the value
side is still walked for nested string-keyed PII.

**Defaults:** `email`, `phone`, `first_name`, `last_name`, `phone_number`, `street_address`, `postal_code`.

The `pii_fields` constructor parameter **replaces** these defaults — it is *not* additive. Operators who want to keep the documented PII names AND add their own should include the defaults in their custom list:

```python
tracker = UCPAnalyticsTracker(
    project_id="my-gcp-project",
    redact_pii=True,
    pii_fields=[
        # Keep the documented defaults...
        "email", "phone", "first_name", "last_name",
        "phone_number", "street_address", "postal_code",
        # ...and add merchant-specific PII keys.
        "ssn",
        "dev.merchant.account_number",
    ],
)
```

The force-included keys (next section) are always OR'd in after the operator's list, so they cannot be accidentally turned off.

### Force-included PII keys

Four documented PII keys are **always** redacted regardless of operator config
so a custom `pii_fields` list cannot accidentally disable safety:

| Key | Why force-included |
|---|---|
| `merchant_authorization` | AP2 detached JWS credential (RFC 7515 App F) |
| `checkout_mandate` | AP2 SD-JWT+kb credential |
| `dev.ucp.buyer_ip` | Documented PII signal per `signals.json` (IP address) |
| `dev.ucp.user_agent` | Documented PII signal per `signals.json` (UA string) |

### Opt-in raw capture (`include_ap2_raw`, `include_signals_raw`)

Both `body.ap2` and `body.signals` carry sensitive data. Safe-default columns
observe only non-PII metadata (presence flags, key names, JOSE headers,
SHA-256 hashes). The full objects land in `ap2_mandate_raw_json` /
`signals_json` only when the operator opts in:

```python
tracker = UCPAnalyticsTracker(
    project_id="my-gcp-project",
    include_ap2_raw=True,        # for forensic mandate inspection
    include_signals_raw=True,    # for forensic signal inspection
)
```

Raw capture **always** runs through `_redact`, regardless of `redact_pii`
— credential and PII-signal values are scrubbed before serialization
even when general PII redaction is off.

---

## Dual Capture

When both the **server middleware** and the **client hook** are active (e.g., during
integration testing or in a platform that both serves and consumes UCP), each UCP
operation produces **two events** — one from each side. This is expected behavior
and useful for comparing server-side vs. client-side latency.

To query only one side, filter by `app_name`:

```sql
-- Server-side events only
SELECT * FROM `project.ucp_analytics.ucp_events`
WHERE app_name = 'flower_shop';

-- Client-side events only
SELECT * FROM `project.ucp_analytics.ucp_events`
WHERE app_name = 'shopping_agent';
```

---

## Advanced Analytics Queries

### Checkout Funnel (Daily Conversion)

```sql
SELECT
    DATE(timestamp) AS day,
    COUNT(CASE WHEN event_type = 'checkout_session_created'   THEN 1 END) AS started,
    COUNT(CASE WHEN event_type = 'checkout_session_updated'   THEN 1 END) AS updated,
    COUNT(CASE WHEN event_type = 'checkout_session_completed' THEN 1 END) AS completed,
    COUNT(CASE WHEN event_type = 'checkout_session_canceled'  THEN 1 END) AS canceled,
    SAFE_DIVIDE(
        COUNT(CASE WHEN event_type = 'checkout_session_completed' THEN 1 END),
        COUNT(CASE WHEN event_type = 'checkout_session_created'   THEN 1 END)
    ) AS conversion_rate
FROM `project.ucp_analytics.ucp_events`
GROUP BY day
ORDER BY day DESC;
```

### Revenue by Merchant

```sql
SELECT
    merchant_host,
    COUNT(DISTINCT checkout_session_id) AS transactions,
    SUM(total_amount) / 100.0 AS revenue_dollars,
    AVG(total_amount) / 100.0 AS avg_order_value,
    currency
FROM `project.ucp_analytics.ucp_events`
WHERE event_type = 'checkout_session_completed'
GROUP BY merchant_host, currency
ORDER BY revenue_dollars DESC;
```

### Latency Percentiles by Operation

```sql
SELECT
    event_type,
    COUNT(*) AS calls,
    APPROX_QUANTILES(latency_ms, 100)[OFFSET(50)] AS p50_ms,
    APPROX_QUANTILES(latency_ms, 100)[OFFSET(95)] AS p95_ms,
    APPROX_QUANTILES(latency_ms, 100)[OFFSET(99)] AS p99_ms,
    MAX(latency_ms) AS max_ms
FROM `project.ucp_analytics.ucp_events`
WHERE latency_ms IS NOT NULL
GROUP BY event_type
ORDER BY p95_ms DESC;
```

### Payment Handler Mix

```sql
SELECT
    payment_handler_id,
    payment_brand,
    COUNT(*) AS transactions,
    SUM(total_amount) / 100.0 AS revenue_dollars,
    AVG(latency_ms) AS avg_latency_ms
FROM `project.ucp_analytics.ucp_events`
WHERE event_type = 'checkout_session_completed'
  AND payment_handler_id IS NOT NULL
GROUP BY payment_handler_id, payment_brand
ORDER BY transactions DESC;
```

### Capability Adoption Across Merchants

```sql
SELECT
    JSON_VALUE(cap, '$.name') AS capability_name,
    JSON_VALUE(cap, '$.version') AS capability_version,
    COUNT(DISTINCT merchant_host) AS merchant_count,
    COUNT(DISTINCT checkout_session_id) AS session_count
FROM `project.ucp_analytics.ucp_events`,
    UNNEST(JSON_QUERY_ARRAY(capabilities_json)) AS cap
WHERE capabilities_json IS NOT NULL
GROUP BY capability_name, capability_version
ORDER BY session_count DESC;
```

### Error Analysis

```sql
SELECT
    error_code,
    error_severity,
    error_message,
    COUNT(*) AS occurrences,
    COUNT(DISTINCT checkout_session_id) AS affected_sessions,
    COUNT(DISTINCT merchant_host) AS affected_merchants
FROM `project.ucp_analytics.ucp_events`
WHERE error_code IS NOT NULL
GROUP BY error_code, error_severity, error_message
ORDER BY occurrences DESC;
```

### Discount Effectiveness

```sql
SELECT
    JSON_VALUE(code) AS discount_code,
    COUNT(*) AS usage_count,
    SUM(discount_amount) / 100.0 AS total_discount_dollars,
    SUM(total_amount) / 100.0 AS total_revenue_dollars,
    AVG(discount_amount) / NULLIF(AVG(total_amount), 0) AS avg_discount_pct
FROM `project.ucp_analytics.ucp_events`,
    UNNEST(JSON_QUERY_ARRAY(discount_codes_json)) AS code
WHERE event_type = 'checkout_session_completed'
  AND discount_codes_json IS NOT NULL
GROUP BY discount_code
ORDER BY usage_count DESC;
```

### Session Timeline (Debug a Specific Checkout)

```sql
SELECT
    timestamp,
    event_type,
    checkout_status,
    http_method,
    http_path,
    http_status_code,
    total_amount,
    error_code,
    error_message,
    latency_ms
FROM `project.ucp_analytics.ucp_events`
WHERE checkout_session_id = 'SESSION_ID_HERE'
ORDER BY timestamp;
```

### Discovery-to-Checkout Conversion

```sql
WITH discovery AS (
    SELECT merchant_host, DATE(timestamp) AS day, COUNT(*) AS profile_fetches
    FROM `project.ucp_analytics.ucp_events`
    WHERE event_type = 'profile_discovered'
    GROUP BY merchant_host, day
),
checkouts AS (
    SELECT merchant_host, DATE(timestamp) AS day, COUNT(*) AS checkout_starts
    FROM `project.ucp_analytics.ucp_events`
    WHERE event_type = 'checkout_session_created'
    GROUP BY merchant_host, day
)
SELECT
    d.merchant_host,
    d.day,
    d.profile_fetches,
    COALESCE(c.checkout_starts, 0) AS checkout_starts,
    SAFE_DIVIDE(c.checkout_starts, d.profile_fetches) AS conversion_rate
FROM discovery d
LEFT JOIN checkouts c USING (merchant_host, day)
ORDER BY d.day DESC;
```

### Fulfillment Geography

```sql
SELECT
    fulfillment_destination_country,
    fulfillment_type,
    COUNT(*) AS orders,
    SUM(total_amount) / 100.0 AS revenue_dollars,
    SUM(fulfillment_amount) / 100.0 AS total_fulfillment_dollars
FROM `project.ucp_analytics.ucp_events`
WHERE event_type = 'checkout_session_completed'
  AND fulfillment_destination_country IS NOT NULL
GROUP BY fulfillment_destination_country, fulfillment_type
ORDER BY orders DESC;
```

---

## Dashboard Visualization

### Looker Studio

Connect BigQuery directly to Looker Studio for real-time dashboards.
Recommended charts:

| Chart | Data Source | Key Metrics |
|---|---|---|
| Checkout funnel bar chart | Daily funnel query | created → updated → completed |
| Revenue time series | Revenue by merchant query | GMV, AOV |
| Latency heatmap | Latency percentiles query | p50, p95, p99 by event type |
| Payment pie chart | Payment handler mix query | Transactions by handler + brand |
| Error table | Error analysis query | Error code, severity, count |
| Geography map | Fulfillment geography query | Orders by country |

### Pre-built Queries

See [`dashboards/queries.sql`](../dashboards/queries.sql) for 10 ready-to-use
BigQuery queries covering all of the above plus capability adoption, escalation
rate, and session timeline debugging.

---

## Examples

Eight runnable examples are included in the [`examples/`](../examples/) directory,
covering every UCP event type (the canonical list lives in `src/ucp_analytics/events.py::UCPEventType`; the demos enumerate it directly so coverage stays in sync as new types land):

| Example | BigQuery? | Transport | Purpose |
|---|---|---|---|
| `e2e_demo.py` | No (SQLite) | REST | Checkout happy path (no GCP needed) |
| `scenarios_demo.py` | Yes (BigQuery) | REST | Errors, cancellation, escalation |
| `cart_demo.py` | Yes (BigQuery) | REST | Cart lifecycle + checkout conversion |
| `order_lifecycle_demo.py` | Yes (BigQuery) | REST | Order delivered/returned/canceled |
| `transport_demo.py` | Yes (BigQuery) | REST/MCP/A2A | All 3 transport comparisons |
| `identity_payment_demo.py` | Yes (BigQuery) | REST | Identity linking + payment flows |
| `bq_demo.py` | Yes | REST/MCP/A2A | Comprehensive — every event type, 3 transports, SDK models, BQ verification |
| `bq_adk_demo.py` | Yes | ADK/MCP/A2A | Comprehensive ADK — every event type, 3 transports, SDK models, BQ verification |

### Quick Start (No GCP)

```bash
pip install fastapi uvicorn httpx
python examples/e2e_demo.py
```

### Quick Start (BigQuery)

```bash
gcloud auth application-default login
uv sync --all-extras
# Edit PROJECT_ID in examples/_demo_utils.py
uv run python examples/scenarios_demo.py         # errors + edge cases
uv run python examples/cart_demo.py              # cart lifecycle
uv run python examples/order_lifecycle_demo.py   # order lifecycle
uv run python examples/transport_demo.py         # REST vs MCP vs A2A
uv run python examples/identity_payment_demo.py  # identity + payment
```

### BigQuery E2E Demo

```bash
gcloud auth application-default login
uv sync --extra fastapi
# Edit PROJECT_ID in examples/bq_demo.py
uv run python examples/bq_demo.py
```

### ADK BigQuery Demo

```bash
gcloud auth application-default login
uv sync --all-extras
# Edit PROJECT_ID in examples/bq_adk_demo.py
uv run python examples/bq_adk_demo.py
```

See [`examples/README.md`](../examples/README.md) for detailed step-by-step
instructions, expected output, and verification queries.

---

## Cleanup

Delete only demo data (preserves production data):

```sql
DELETE FROM `project.ucp_analytics.ucp_events`
WHERE app_name IN ('bq_demo', 'bq_adk_demo');
```

Drop the entire table:

```sql
DROP TABLE IF EXISTS `project.ucp_analytics.ucp_events`;
```

---

## Feedback & Resources

- [UCP Specification](https://github.com/Universal-Commerce-Protocol/ucp)
- [UCP Developer Docs](https://ucp.dev)
- [Design Doc](design_doc.md)
- [Dashboard Queries](../dashboards/queries.sql)
- [GitHub Issues](https://github.com/haiyuan-eng-google/data-agent-kit/issues)
  (in the Data Agent Kit repository)
