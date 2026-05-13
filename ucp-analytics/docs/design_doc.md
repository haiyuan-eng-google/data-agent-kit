# UCP Analytics — Design Document

**Author:** Haiyuan Cao
**Status:** Released
**Version:** 0.2.0
**Date:** May 11, 2026
**Spec target:** UCP [`c5c6139`](https://github.com/Universal-Commerce-Protocol/ucp/commit/c5c6139) (2026-05-06)
**Repository:** [haiyuan-eng-google/data-agent-kit](https://github.com/haiyuan-eng-google/data-agent-kit) — `ucp-analytics/`

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Architecture](#3-architecture)
4. [Event Classification](#4-event-classification)
5. [BigQuery Schema](#5-bigquery-schema)
6. [Response Parser Design](#6-response-parser-design)
7. [Async BigQuery Writer](#7-async-bigquery-writer)
8. [Integration Patterns](#8-integration-patterns)
9. [Analytics Queries](#9-analytics-queries)
10. [Examples](#10-examples)
11. [Deployment & Configuration](#11-deployment--configuration)
12. [Relationship to Existing Work](#12-relationship-to-existing-work)
13. [Future Work](#13-future-work)

---

## 1. Executive Summary

The Universal Commerce Protocol (UCP) defines standardized APIs for agentic commerce — enabling AI agents to discover merchant capabilities, create checkout sessions, process payments, and manage orders. However, UCP ships with **no built-in observability**. Businesses and platforms have no structured way to track checkout conversion funnels, payment success rates, error patterns, or latency across the protocol surface.

**UCP Analytics** is a new open-source package that automatically captures every UCP operation into BigQuery, providing structured commerce event tracking aligned with the UCP specification. It hooks into the HTTP transport layer (the primary UCP binding) via FastAPI middleware (merchant side) and HTTPX event hooks (agent/platform side), requiring **zero changes to existing UCP server or client code**.

**Key outcomes:**
1. Checkout funnel visibility from discovery to completion
2. Revenue attribution by merchant, payment handler, and fulfillment geography
3. Error and escalation debugging with session replay
4. Latency monitoring per UCP operation
5. Capability adoption tracking across the ecosystem

---

## 2. Problem Statement

### 2.1 The Observability Gap

UCP defines the protocol for commerce but leaves observability entirely to implementers. The reference sample server (flower shop) writes to a local SQLite database for transaction state but provides no analytics, no event stream, and no dashboard. As UCP adoption scales, every merchant, platform, and payment handler must independently build:

- Checkout funnel tracking (sessions created vs. completed vs. abandoned)
- Error classification (recoverable vs. escalation vs. fatal)
- Payment handler performance (success rates, latency by handler/brand)
- Fulfillment geography analysis (order destinations, shipping methods)
- Capability adoption metrics (which extensions do merchants implement)

### 2.2 Why This Matters Now

UCP was publicly launched in January 2026, co-developed by Google, Shopify, Etsy, Walmart, Target, Wayfair, and endorsed by 20+ partners including Adyen, Mastercard, Visa, and Stripe. With Google AI Mode in Search and Gemini app providing the first consumer surfaces, UCP transaction volume is growing rapidly. Without standardized analytics, debugging requires ad-hoc log parsing and business intelligence requires custom ETL per merchant.

### 2.3 Non-Goals

- Real-time alerting (use existing GCP monitoring on top of BigQuery)
- PCI-DSS compliant payment storage (only handler IDs and card brands captured, not tokens/credentials)
- Replacing merchant transaction databases (analytics layer, not system of record)
- Non-HTTP transports beyond the current JSON-RPC classification (e.g. native gRPC bindings)

---

## 3. Architecture

### 3.1 System Overview

UCP Analytics hooks into the transport layer as a passive observer: intercepting requests/responses without modifying them, extracting structured UCP fields, and writing batched event rows to BigQuery. It supports three transports:

- **REST** — HTTP method + path + response body classification (primary binding)
- **MCP** — JSON-RPC tool name classification via `record_jsonrpc(transport="mcp")`
- **A2A** — JSON-RPC tool name classification via `record_jsonrpc(transport="a2a")`

```
Platform (Agent)                    Business (Merchant)
+------------------+                +--------------------+
| HTTPX Client     |                | FastAPI Server     |
| + EventHook   -----------REST----------> + Middleware  |
+--------+---------+                +---------+----------+
         |                                    |
         +------------------+-----------------+
                            v
                  UCPAnalyticsTracker
                  +--UCPResponseParser   (classify + extract)
                  +--classify_jsonrpc()  (MCP/A2A tool mapping)
                  +--AsyncBigQueryWriter (batch + flush)
                            |
                            v
                       BigQuery
                  PARTITION BY timestamp
                  CLUSTER BY event_type,
                    checkout_session_id, merchant_host
```

### 3.2 Integration Points

| Integration | Side | Mechanism | Use Case |
|---|---|---|---|
| **FastAPI Middleware** | Merchant server | ASGI middleware on Starlette | Track all inbound UCP requests |
| **HTTPX Event Hook** | Agent / platform | httpx response event hook | Track all outbound UCP calls |
| **ADK Plugin** (optional) | ADK agent | BasePlugin before/after callbacks | For ADK-based commerce agents |
| **JSON-RPC recorder** | MCP / A2A agent | `tracker.record_jsonrpc()` | Track MCP and A2A tool calls |
| **Direct API** | Any | `tracker.record_http()` / `tracker.record_event()` | Custom integrations, testing |

### 3.3 Data Flow

1. The middleware or event hook captures raw HTTP method, path, status code, request body, and response body.
2. `UCPResponseParser.classify()` maps the HTTP operation to a UCP event type using strict regex matching on well-known UCP paths (e.g. `/checkout-sessions`, `/orders`, `/.well-known/ucp`, `/webhooks`). For webhook paths, the classifier accepts an optional `request_body` parameter since the order payload is in the request body (the response is just an ack like `{"status": "ok"}`).
3. `UCPResponseParser.extract()` parses the UCP JSON response to extract structured fields: session ID, status, totals (all 7 spec types), line items, payment instruments/handlers, fulfillment, capabilities, extensions, discount codes/applied, checkout metadata (expires_at, continue_url), order details (permalink_url, fulfillment expectations/events), and errors.
4. The event is serialized into a flat BigQuery row and enqueued in the `AsyncBigQueryWriter` buffer.
5. When buffer reaches `batch_size` (default 50), rows flush to BigQuery via streaming insert (run in a background thread via `asyncio.to_thread()` to avoid blocking the event loop). On `tracker.close()`, any in-flight fire-and-forget recording tasks are awaited first, then remaining buffered rows are flushed.

### 3.4 Lazy Loading and Optional Dependencies

The core module depends only on `google-cloud-bigquery` and `httpx`. Optional integrations are lazy-loaded to avoid import errors:

- **FastAPI middleware:** `UCPAnalyticsMiddleware` is exposed via `__getattr__` in `__init__.py` and only imported when accessed, so the core code works without `starlette` installed.
- **ADK plugin:** `adk_plugin.py` uses `try/except ImportError` around `google.adk` imports, falling back to `object` as the base class when ADK is not installed.

---

## 4. Event Classification

### 4.1 Event Type Mapping (32 types)

Events are automatically classified from HTTP method + path + response body. Path matching uses strict regex patterns to avoid false positives (e.g. `/orders` matches but `/reorder` does not). For MCP/A2A transports, `classify_jsonrpc()` maps tool names to the same event types.

Webhook detection in 0.2.0 layers two signals on top of path matching:
- An operator-configured `webhook_path_prefixes` list on the tracker for platforms that publish webhook URLs other than `/webhook(s)` (UCP `order.md`: *"The URL format is platform-specific"*).
- A Standard Webhooks header fallback (`Webhook-Id` + `Webhook-Timestamp` together) for unknown URLs — suppressed on known non-webhook UCP paths so a malicious header-stamp on `/checkout-sessions` can't override URL semantics.

The webhook branch runs before the `/orders` REST branch in the classifier, so a platform that publishes `/webhooks/orders` correctly classifies as a webhook delivery rather than a REST `/orders` endpoint.

#### Checkout (6)

| HTTP Operation | Event Type | Trigger |
|---|---|---|
| `POST /checkout-sessions` | `checkout_session_created` | New checkout session initiated |
| `GET /checkout-sessions/{id}` | `checkout_session_get` | Session retrieved for display/validation |
| `PUT /checkout-sessions/{id}` | `checkout_session_updated` | Buyer info, fulfillment, or discount added |
| `PUT /checkout-sessions/{id}` | `checkout_escalation` | Response status = `requires_escalation` |
| `POST /checkout-sessions/{id}/complete` | `checkout_session_completed` | Checkout finalized with payment |
| `POST /checkout-sessions/{id}/cancel` | `checkout_session_canceled` | Session explicitly canceled |

#### Cart (4)

| HTTP Operation | Event Type | Trigger |
|---|---|---|
| `POST /carts` | `cart_created` | New cart created |
| `GET /carts/{id}` | `cart_get` | Cart retrieved |
| `PUT /carts/{id}` | `cart_updated` | Cart updated (items added/removed) |
| `POST /carts/{id}/cancel` | `cart_canceled` | Cart explicitly canceled |

#### Catalog (3)

| HTTP Operation | Event Type | Trigger |
|---|---|---|
| `POST /catalog/search` | `catalog_search` | Catalog search query |
| `POST /catalog/lookup` | `catalog_lookup` | Catalog item lookup by ID |
| `POST /catalog/product` | `catalog_product_get` | Single product detail fetch |

#### Order (8)

| HTTP Operation | Event Type | Trigger |
|---|---|---|
| `POST /orders` | `order_created` | New order created |
| `GET /orders/{id}` *(no lifecycle)* | `order_get` | Read-only poll of order state |
| `PUT /orders/{id}` *(no lifecycle)* | `order_updated` | REST-driven order mutation |
| `GET`/`PUT` `/orders/{id}` *(fulfillment.events[].type=shipped or in_transit)* | `order_shipped` | Latest shipment event is shipped / in_transit |
| `GET`/`PUT` `/orders/{id}` *(...=delivered)* | `order_delivered` | Latest shipment event is delivered |
| `GET`/`PUT` `/orders/{id}` *(...=returned_to_sender or adjustments[].type=refund/return)* | `order_returned` | Return processed |
| `GET`/`PUT` `/orders/{id}` *(...=canceled/undeliverable or adjustments[].type=cancellation)* | `order_canceled` | Order canceled |
| Webhook delivery without recognizable lifecycle | `order_webhook_received` | Webhook detected (path prefix or header pair) but body has no recognizable lifecycle status |
| `POST /webhooks/order-delivered` | `order_delivered` | Legacy URL-segment fallback (body-driven wins when present) |
| `POST /webhooks/order-returned` | `order_returned` | Legacy URL-segment fallback |
| `POST /webhooks/order-canceled` | `order_canceled` | Legacy URL-segment fallback |

**Lifecycle derivation priority (B8, c5c6139 schema):** At `c5c6139`, `order.json` no longer carries a top-level `status`. Lifecycle is derived from (1) the latest `fulfillment.events[].type` by RFC 3339 `occurred_at`, (2) the latest `adjustments[].type` if no fulfillment event matches, then (3) legacy top-level `status` for pre-c5c6139 senders. Lifecycle wins on either GET or PUT — a GET that returns a `delivered` event still classifies as `order_delivered`.

**Webhook payload location:** For webhook paths, the order payload is in the **request** body (the response is typically an ack like `{"status": "ok"}`). The classifier and tracker use `request_body` for both classification and field extraction on webhook paths. Webhook 4xx/5xx responses classify as `error` rather than falling through to `order_webhook_received`.

#### Identity (3)

| HTTP Operation | Event Type | Trigger |
|---|---|---|
| `POST /identity`, `/oauth2/authorize` | `identity_link_initiated` | OAuth identity linking started |
| `GET /identity/callback`, `/oauth2/token` | `identity_link_completed` | OAuth callback / token endpoint finalizes the link |
| `POST /identity/revoke`, `/oauth2/revoke`, `DELETE /identity/*` | `identity_link_revoked` | Identity link revoked |

OAuth metadata discovery endpoints — `/.well-known/oauth-authorization-server` (RFC 8414), `/.well-known/openid-configuration` (OIDC Discovery), and `/.well-known/oauth-protected-resource` (RFC 9728) — classify as `identity_link_initiated` since they're the first step of an identity-linking flow.

#### Payment (4)

| Event Type | Trigger |
|---|---|
| `payment_handler_negotiated` | Platform + merchant handler intersection computed |
| `payment_instrument_selected` | Buyer selects payment instrument |
| `payment_completed` | Payment succeeds |
| `payment_failed` | Payment fails |

#### Discovery (2)

| HTTP Operation | Event Type | Trigger |
|---|---|---|
| `GET /.well-known/ucp` | `profile_discovered` | Agent fetches merchant discovery profile |
| Capability exchange | `capability_negotiated` | UCP capability negotiation completed |

#### Fallback (2)

| Condition | Event Type | Trigger |
|---|---|---|
| Any unmatched path, status >= 400 | `error` | HTTP error response |
| Any unmatched path, status < 400 | `request` | Unclassified successful request |

**Note:** Path-specific matches take priority over status code. A `POST /checkout-sessions` returning 500 is classified as `checkout_session_created` (not `error`), since the path match is more informative for analytics.

### 4.2 JSON-RPC Classification (MCP/A2A)

For MCP and A2A transports, `classify_jsonrpc()` maps tool names to event types using pattern matching. Examples:

| Tool Name Pattern | Event Type |
|---|---|
| `discover_merchant`, `a2a.ucp.discover` | `profile_discovered` |
| `create_checkout`, `a2a.ucp.checkout.create` | `checkout_session_created` |
| `complete_checkout`, `a2a.ucp.checkout.complete` | `checkout_session_completed` |
| `add_to_checkout`, `remove_from_checkout`, `update_customer_details` | `checkout_session_updated` |
| `start_payment` | `checkout_session_updated` (pre-completion step) |
| `create_cart`, `a2a.ucp.cart.create` | `cart_created` |
| `get_order`, `a2a.ucp.order.get` | `order_get` (refined by lifecycle from the response body) |
| `order_event_webhook` | *(by request body status)* |
| `link_identity`, `a2a.ucp.identity.link` | `identity_link_initiated` |
| `negotiate_capability`, `a2a.ucp.capability.negotiate` | `capability_negotiated` |

The response body is still parsed for field extraction (totals, status, payment, etc.) regardless of transport.

### 4.3 UCP Checkout State Machine Alignment

```
incomplete --> requires_escalation --> ready_for_complete
     |              |                        |
     v              v                        v
  canceled       canceled             complete_in_progress
                                             |
                                             v
                                         completed
```

Each state transition generates a corresponding analytics event, enabling precise funnel analysis.

---

## 5. BigQuery Schema

**Table:** `{project}.{dataset}.ucp_events`
**Partitioned by:** `timestamp` (daily)
**Clustered by:** `event_type`, `checkout_session_id`, `merchant_host`

### Identity & Context

| Column | Type | Description |
|---|---|---|
| `event_id` | STRING (PK) | UUID v4, unique per event |
| `event_type` | STRING | Classified UCP event type |
| `timestamp` | TIMESTAMP | UTC event time (partition key) |
| `app_name` | STRING | Application name tag |
| `merchant_host` | STRING | Business endpoint hostname |
| `transport` | STRING | `rest` \| `mcp` \| `a2a` \| `embedded` |
| `platform_profile_url` | STRING | Raw `UCP-Agent` header (legacy; superseded by `ucp_agent_profile_url`) |
| `ucp_agent_profile_url` | STRING | `profile` member parsed out of the RFC 8941 `UCP-Agent` Structured Field Dictionary |

### UCP Request-Body Context

Captured from `body.context` on requests carrying a UCP Context object (e.g. checkout-create, catalog-search).

| Column | Type | Description |
|---|---|---|
| `context_intent` | STRING | Buyer intent / agent task |
| `context_language` | STRING | BCP 47 language tag |
| `context_currency` | STRING | ISO 4217 currency code |
| `context_eligibility_json` | JSON | Eligibility claim payload |

### UCP Checkout Fields

| Column | Type | Description |
|---|---|---|
| `checkout_session_id` | STRING | UCP checkout session ID (cluster key) |
| `checkout_status` | STRING | Current status in state machine |
| `order_id` | STRING | Order ID created on completion |
| `order_label` | STRING | Business-set human-readable order label (order-shaped bodies only) |
| `currency` | STRING | ISO 4217 currency code |
| `items_discount_amount` | INTEGER | SUM of `totals[type=items_discount].amount` |
| `subtotal_amount` | INTEGER | SUM of `totals[type=subtotal].amount` |
| `discount_amount` | INTEGER | SUM of `totals[type=discount].amount` |
| `fulfillment_amount` | INTEGER | SUM of `totals[type=fulfillment].amount` |
| `tax_amount` | INTEGER | SUM of `totals[type=tax].amount` (handles split state+local) |
| `fee_amount` | INTEGER | SUM of `totals[type=fee].amount` |
| `total_amount` | INTEGER | SUM of `totals[type=total].amount` |
| `totals_json` | JSON | Full ordered `totals[]` verbatim (duplicates, `display_text`, `lines[]`, business-defined types) |
| `line_item_count` | INTEGER | Number of items in checkout |
| `line_items_json` | JSON | Full line items array |
| `discount_codes_json` | JSON | Discount codes from discount extension |
| `discount_applied_json` | JSON | Applied discounts from discount extension |
| `expires_at` | STRING | Checkout session expiration timestamp |
| `continue_url` | STRING | URL to continue checkout (captures `error_response.continue_url` too) |
| `permalink_url` | STRING | Permanent link to the order |

### Order Lifecycle (c5c6139 shape)

| Column | Type | Description |
|---|---|---|
| `fulfillment_events_json` | JSON | Full `fulfillment.events[]` array verbatim |
| `adjustments_json` | JSON | Full `adjustments[]` array verbatim |
| `latest_fulfillment_event_type` | STRING | Type of the latest fulfillment event by RFC 3339 `occurred_at` |
| `latest_fulfillment_event_at` | TIMESTAMP | `occurred_at` of the latest fulfillment event |
| `latest_adjustment_type` | STRING | Type of the latest adjustment (refund / cancellation / etc.) |
| `latest_adjustment_status` | STRING | `pending` / `completed` / `failed` |
| `latest_adjustment_at` | TIMESTAMP | `occurred_at` of the latest adjustment |

### HTTP Message Signing (RFC 9421 + UCP `signatures.md`)

Three-state nullable BOOLs distinguish "headers never observed" (NULL) from "observed and unsigned" (FALSE).

| Column | Type | Description |
|---|---|---|
| `request_signed` | BOOL | True iff request carries a complete `Signature-Input` + `Signature` pair |
| `response_signed` | BOOL | Same on the response side |
| `request_signature_keyid` | STRING | First `keyid` parsed from `Signature-Input` (captured even on half-signed for forensics) |
| `response_signature_keyid` | STRING | Same on the response side |
| `request_signature_alg` | STRING | JWA algorithm (`ES256` / `ES384`) derived from the matched JWK's `crv` via `jwk_lookup`; populated only when `request_signed=True` |
| `response_signature_alg` | STRING | Same on the response side |

### Webhook Metadata (Standard Webhooks)

| Column | Type | Description |
|---|---|---|
| `webhook_id` | STRING | `Webhook-Id` request header (webhook-scoped) |
| `webhook_timestamp` | TIMESTAMP | `Webhook-Timestamp` parsed from Unix seconds into ISO 8601 UTC |

### WWW-Authenticate Bearer Challenge (RFC 7235 / 6750 / 9728)

| Column | Type | Description |
|---|---|---|
| `auth_challenge_realm` | STRING | `realm` auth-param |
| `auth_challenge_error` | STRING | `error` auth-param (`invalid_token` / `insufficient_scope` / ...) |
| `auth_challenge_scope` | STRING | `scope` auth-param |
| `auth_challenge_resource_metadata` | STRING | `resource_metadata` pointer per RFC 9728 |

### Embedded Checkout (server-observable slice)

| Column | Type | Description |
|---|---|---|
| `embedded_delegations_json` | JSON | Union of `delegate[]` across all embedded services in `/.well-known/ucp` |
| `embedded_color_schemes_json` | JSON | Union of `color_scheme[]` across all embedded services |
| `embedded_ec_color_scheme` | STRING | `ec_color_scheme` query parameter on the request URL / path |

### AP2 Mandates + Buyer Consent

Safe-by-default columns capture only non-PII metadata; the raw column is opt-in via `include_ap2_raw=True`.

| Column | Type | Description |
|---|---|---|
| `ap2_mandate_present` | BOOL | Whether any AP2 mandate field is present on `body.ap2` |
| `ap2_mandate_keys_json` | JSON | Names of present mandate fields |
| `ap2_mandate_metadata_json` | JSON | JOSE header (`kid` / `alg` / `typ`) + SHA-256 hex of each credential string. Payload / disclosures **never** decoded. |
| `buyer_consent_json` | JSON | `body.buyer.consent` filtered to the four documented boolean flags (analytics / preferences / marketing / sale_of_data). Never the parent buyer object's PII. |
| `ap2_mandate_raw_json` | JSON | (Opt-in) original `body.ap2` after `_redact`; credential field names force-included in redaction |

### Authorization Signals

Safe-by-default columns capture only key names; the raw column is opt-in via `include_signals_raw=True`.

| Column | Type | Description |
|---|---|---|
| `signals_present` | BOOL | Whether `body.signals` carries any entries |
| `signals_keys_json` | JSON | Names of signal keys (never values, which may carry IP / UA / fingerprint data) |
| `signals_json` | JSON | (Opt-in) original `body.signals` after `_redact`; `dev.ucp.buyer_ip` and `dev.ucp.user_agent` force-included in redaction |

### Payment, Capabilities, Fulfillment, & Errors

| Column | Type | Description |
|---|---|---|
| `payment_handler_id` | STRING | Payment handler ID (e.g. `com.stripe.payment`) |
| `payment_instrument_type` | STRING | `card`, `wallet`, `bank_transfer`, etc. |
| `payment_brand` | STRING | `Visa`, `Mastercard`, etc. |
| `payment_available_instruments_json` | JSON | Per-handler `available_instruments[]` from `body.ucp.payment_handlers[*]` (preserved per-handler) |
| `ucp_version` | STRING | Protocol version from `ucp` envelope |
| `capabilities_json` | JSON | Capabilities array from `ucp.capabilities` |
| `extensions_json` | JSON | Extensions metadata |
| `fulfillment_type` | STRING | `shipping`, `pickup`, `digital`, etc. |
| `fulfillment_destination_country` | STRING | ISO 3166-1 alpha-2 country code |
| `error_code` | STRING | First error's `code` from `messages[]` |
| `error_message` | STRING | First error's `content` |
| `error_severity` | STRING | First error's `severity` |
| `messages_json` | JSON | Full `messages[]` array verbatim |
| `message_info_codes_json` | JSON | Dedup'd, order-preserved list of info-severity codes |
| `message_warning_codes_json` | JSON | Dedup'd, order-preserved list of warning-severity codes |
| `identity_optional_present` | BOOL | Three-state convenience flag — TRUE when `identity_optional` is in info codes, FALSE when info codes are observed but it isn't, NULL when no info codes |
| `eligibility_accepted_present` | BOOL | Three-state outcome flag — denominator is "any eligibility outcome code observed" |
| `eligibility_not_accepted_present` | BOOL | Same denominator |
| `eligibility_invalid_present` | BOOL | Same denominator (cross-severity capture; `eligibility_invalid` is canonically error) |
| `latency_ms` | FLOAT | Request-to-response latency in milliseconds |
| `custom_metadata_json` | JSON | User-defined static key-value metadata on every event |

---

## 6. Response Parser Design

### 6.1 Field Extraction Logic

`UCPResponseParser.extract()` understands the UCP checkout object schema:

- **Totals array parsing:** UCP represents financial data as a typed `totals` array. Each entry has `type` and `amount` in minor units. The parser handles all 7 spec-defined well-known total types — `items_discount`, `subtotal`, `discount`, `fulfillment`, `tax`, `fee`, `total` — and SUM-aggregates over duplicate entries of the same type (split state+local tax, multi-line discount). `total.json` is an open vocabulary; business-defined types are dropped from the scalar columns but preserved verbatim in `totals_json` JSON. Amounts are signed integers per `signed_amount.json` (negative for refunds); non-int values (string / float / bool) are dropped from SUM but preserved in `totals_json` for signal fidelity.
- **Payment extraction:** The SDK `PaymentResponse` contains both `handlers[]` (merchant payment handler configs) and `instruments[]` (buyer payment methods). Instruments are preferred for analytics since they carry `handler_id`, `type`, and `brand`. Discovery responses additionally surface `payment_available_instruments_json` from `body.ucp.payment_handlers[*].available_instruments` (per-handler arrays preserved intact). Extracts handler_id, instrument type, and brand — never captures credentials/tokens.
- **Capability detection:** Extracts the UCP metadata envelope (`ucp.version`, `ucp.capabilities`). Per the Python SDK and samples, capabilities are arrays of objects with a `name` field (e.g., `[{"name": "dev.ucp.shopping.checkout", "version": "2026-01-11"}]`). For robustness, also handles an object-keyed format where capability names are dict keys.
- **Embedded transport config:** Walks `ucp.services[*]` for `transport: embedded` entries; unions `delegate[]` and `color_scheme[]` across all embedded services into `embedded_delegations_json` and `embedded_color_schemes_json`.
- **Discount extension:** Extracts `discounts.codes` and `discounts.applied` into `discount_codes_json` and `discount_applied_json` columns.
- **Order lifecycle (c5c6139):** `order.json` no longer carries a top-level `status`. Lifecycle derivation walks (1) `fulfillment.events[]` and (2) `adjustments[]`, picking the entry with the latest RFC 3339 `occurred_at` via aware datetime parsing (mixed `Z` / `±HH:MM` offsets compared as instants, not lexicographically; naive timestamps treated as invalid). Falls back to legacy top-level `status` for pre-c5c6139 senders. Surfaces full arrays plus `latest_*_type` / `latest_*_at` / `latest_adjustment_status` scalars.
- **Checkout metadata:** Extracts `expires_at` and `continue_url` (the same column captures `error_response.continue_url` per `error_response.json`).
- **Order model:** Extracts `checkout.order` as a nested object (not flat `order_id`), including `permalink_url`. The optional business-set `order.label` (PR #326 upstream) surfaces only on order-shaped bodies (those carrying `checkout_id`) so a stray `label` on a checkout body doesn't misattribute.
- **Session-order correlation:** Distinguishes checkout sessions from orders by checking for `checkout_id` (present on orders, absent on checkouts).
- **Checkout status scoping:** The `checkout_status` field is only populated for actual checkout responses, not order or cart responses. This uses two guards: (1) bodies with `checkout_id` are orders (skipped), and (2) the status value must be a known checkout status (`incomplete`, `requires_escalation`, `ready_for_complete`, `complete_in_progress`, `completed`, `canceled`).
- **Messages:** Single pass over `messages[]` captures the first error (legacy `error_code`/`error_message`/`error_severity`) plus dedup'd, order-preserved per-severity code lists. Three-state nullable convenience flags derive from `messages[].code`: `identity_optional_present` (info-severity), `eligibility_accepted_present` / `eligibility_not_accepted_present` / `eligibility_invalid_present` (cross-severity capture — `eligibility_invalid` is canonically error).

### 6.2 PII Redaction

`UCPAnalyticsTracker._redact` recursively walks JSON bodies, replacing configured fields with `[REDACTED]` when `key.lower() in self.pii_fields`. Defaults: `email`, `phone`, `first_name`, `last_name`, `phone_number`, `street_address`, `postal_code`. The `pii_fields` constructor parameter **replaces** the defaults wholesale (it is not additive) — operators who want to keep the documented PII names should include them in their custom list. The force-included keys below are OR'd in regardless.

The redaction set has four documented PII keys **force-included** regardless of operator config so a custom `pii_fields` list cannot accidentally disable safety:

- `merchant_authorization` and `checkout_mandate` — AP2 cryptographic credentials (detached JWS / SD-JWT+kb).
- `dev.ucp.buyer_ip` and `dev.ucp.user_agent` — documented PII signal keys per `signals.json`.

Non-string dict keys (int / None / tuple) are handled gracefully — they can't match `pii_fields` (which holds strings) but recursion still walks the value side, so nested string-keyed PII under a non-string key is still caught.

### 6.3 Header-Aware Helpers (`_headers.py`)

Shared header utilities live in `_headers.py` (no Starlette dependency, so the HTTPX hook can use them):

- `is_signed(headers)` — true iff both `Signature-Input` and `Signature` are present (UCP `signatures.md` half-signed protection).
- `signature_keyid(headers)` — first `keyid` parsed from `Signature-Input` (captured even on half-signed for forensics).
- `signature_alg_from_jwk(jwk)` — JWA name (`ES256` / `ES384`) derived from JWK `crv` per UCP `signatures.md`. No fallback to JWK `alg` field on unknown curves — future curves must be explicit additions to the mapping.
- `decode_jose_header(credential)` — decodes ONLY the first dot-separated segment of a JWS / JWT / SD-JWT credential. Restores stripped base64url padding. Never decodes payload or disclosures.
- `credential_sha256(credential)` — opaque hex hash for correlation across rows without persisting the credential itself.
- `parse_bearer_challenge(headers)` — RFC 7235-faithful `WWW-Authenticate` Bearer challenge parser. Handles multi-challenge isolation, multi-line transport preservation, BWS around `=`, token-form values (`error=invalid_token`), and hyphenated scheme names. `_SCHEME_TOKEN_RE` and `_AUTH_PARAM_RE` use RFC 7230 `tchar+` syntax.
- `ucp_agent_profile_url(headers)` — parses the `profile` member from the RFC 8941 `UCP-Agent` Structured Field Dictionary. Anchored on `,` member boundaries (not `;` parameter boundaries) so attacker-controlled parameter values can't smuggle in.
- `webhook_id(headers)` / `webhook_timestamp_iso(headers)` — Standard Webhooks metadata; the latter parses Unix seconds → ISO 8601 UTC string.

### 6.4 Webhook Detection (`_path_match.py`)

`is_webhook_delivery(path, request_headers, extra_prefixes)` is the single source of truth across the classifier, middleware, HTTPX hook, and tracker `is_webhook` gate. Two acceptance branches:

1. Path matches one of the default `/webhook(s)` prefixes or any operator-configured `webhook_path_prefixes` (UCP `order.md`: *"The URL format is platform-specific"*).
2. Standard Webhooks `Webhook-Id` + `Webhook-Timestamp` header pair is present on the request.

Header-based detection is **suppressed** on known non-webhook UCP REST paths (`/checkout-sessions`, `/carts`, `/catalog`, `/orders`, `/identity`, `/oauth2`, `/.well-known/*`, testing/simulate paths) so a buggy or malicious sender stamping webhook headers on `/checkout-sessions` can't trick the classifier.

---

## 7. Async BigQuery Writer

### 7.1 Batching Strategy

`AsyncBigQueryWriter` buffers events in memory and flushes when:
- `batch_size` threshold reached (default: 50)
- `flush()` called explicitly
- `close()` called (shutdown)

### 7.2 Non-Blocking I/O

All synchronous BigQuery client calls (`create_dataset`, `create_table`, `insert_rows_json`) are dispatched via `asyncio.to_thread()` to avoid blocking the event loop. This is critical when the writer is used inside an async web server (FastAPI/Starlette).

### 7.3 Auto-Table Creation

On first write, the writer lazily initializes the BigQuery client, creates dataset and table with full schema (partitioned + clustered). Uses `exists_ok=True` for idempotent setup across multiple processes.

### 7.4 Buffer Safety

- **Async-safe:** All buffer operations (enqueue, flush, re-queue) are protected by an `asyncio.Lock`.
- **Max buffer size:** The writer caps the in-memory buffer at `max_buffer_size` (default: 10,000). When the buffer is full, the oldest event is dropped and a warning is logged. This prevents unbounded memory growth if BigQuery is persistently unreachable.
- **Retry on failure:** If `insert_rows_json` raises an exception (e.g. network error), the entire batch is re-queued to the front of the buffer for retry on next flush. For **partial** insert errors (some rows accepted, some rejected), only the failed rows — identified by their index in the BQ error response — are re-queued. This prevents successful rows from being duplicated. Both paths respect the max buffer size cap.

---

## 8. Integration Patterns

### 8.1 FastAPI Middleware (Merchant Server)

`UCPAnalyticsMiddleware` is a Starlette `BaseHTTPMiddleware` that filters by UCP path prefixes (`/checkout-sessions`, `/.well-known/ucp`, `/orders`, `/carts`, `/identity`, `/testing/simulate`, `/webhooks`). For webhook paths, the tracker uses the request body (which contains the order payload) for both classification and field extraction, since the response is typically just an ack. For matching requests: reads request body, executes handler, captures response, measures latency.

Analytics recording is fire-and-forget: the middleware dispatches `tracker.record_http()` via `asyncio.create_task()` so it does not block the HTTP response. Each task is registered on the tracker via `tracker.register_pending_task()`, allowing `tracker.close()` to drain all in-flight tasks before flushing the buffer. This means the shutdown pattern is simply `await tracker.close()` — no need to reference the middleware instance (which is constructed internally by `app.add_middleware()` and not directly accessible). Response headers (including multi-value headers like `set-cookie`) are preserved using raw header passthrough.

The middleware is lazy-loaded in `__init__.py` via `__getattr__`, so importing the core module does not require `starlette` to be installed.

### 8.2 HTTPX Event Hook (Agent Client)

`UCPClientEventHook` is an async response event hook. Fires after every HTTP response, checks path against UCP patterns, reads response body via `aread()`, records event with `Response.elapsed` for latency.

### 8.3 ADK Plugin Adapter (Optional)

`UCPAgentAnalyticsPlugin` is a thin `BasePlugin` adapter with `before_tool_callback` (start timer) and `after_tool_callback` (classify tool call, extract fields, record event). Tool names are matched against UCP patterns. Timing entries are cleaned up for all tools (not just UCP ones) to prevent memory leaks.

### 8.4 Composability

All three integration points share the same `UCPAnalyticsTracker` and `AsyncBigQueryWriter`. Both middleware and event hook can be active simultaneously — they capture different sides of different HTTP connections without duplicating events.

---

## 9. Analytics Queries

10 ready-to-use BigQuery queries in `dashboards/queries.sql`:

| Query | Description | Key Metric |
|---|---|---|
| Checkout Funnel | Daily conversion rates by stage | created → completed % |
| Revenue by Merchant | Daily revenue, AOV per merchant | SUM(total_amount) |
| Payment Handler Mix | Transactions by handler/brand | Count, revenue, latency |
| Capability Adoption | UNNEST capabilities per merchant | Sessions per capability |
| Error Analysis | Error codes with severity breakdown | Affected sessions |
| Escalation Rate | Human handoff rate per merchant | Escalation / created |
| Latency Percentiles | p50/p95/p99 per operation type | APPROX_QUANTILES |
| Fulfillment Geography | Orders by country and method | Revenue per country |
| Session Timeline | Debug a specific checkout | Event sequence |
| Discovery Hit Rate | Profile fetch → checkout rate | Conversion from discovery |

---

## 10. Examples

Eight runnable examples are included (see [`examples/README.md`](../examples/README.md) for full details):

| Example | BigQuery? | Transport | Coverage |
|---|---|---|---|
| `e2e_demo.py` | No (SQLite) | REST | Checkout happy path (5 types) |
| `scenarios_demo.py` | Yes | REST | Errors, cancellation, escalation (7 types) |
| `cart_demo.py` | Yes | REST | Cart CRUD + checkout conversion (6 types) |
| `order_lifecycle_demo.py` | Yes | REST | Order delivered/returned/canceled (8 types) |
| `transport_demo.py` | Yes | REST/MCP/A2A | All 3 transports compared (5 types) |
| `identity_payment_demo.py` | Yes | REST | Identity linking + payment flows (10 types) |
| `bq_demo.py` | Yes | REST/MCP/A2A | Every event type, 3 transports, BQ verification |
| `bq_adk_demo.py` | Yes | ADK/MCP/A2A | Every event type via ADK plugin, BQ verification |

Shared BigQuery configuration (`PROJECT_ID`, `DATASET_ID`, `TABLE_ID`) lives in `examples/_demo_utils.py` and reads from the `GCP_PROJECT_ID` environment variable.

**Local demo (no GCP):** `e2e_demo.py` starts a mini UCP merchant server (FastAPI, port 8199) with a flower shop catalog, runs a shopping agent through the full happy path (discovery → checkout → payment → shipment), writes 6 events to local SQLite, and prints an analytics report.

**Comprehensive demos:** `bq_demo.py` and `bq_adk_demo.py` each exercise every event type across REST, MCP, and A2A transports, then query BigQuery to verify all events landed correctly. (`src/ucp_analytics/events.py::UCPEventType` is the canonical list — the demos enumerate it directly rather than hard-coding a count, so coverage stays in sync as new event types land.)

---

## 11. Deployment & Configuration

### Configuration Options

| Parameter | Default | Description |
|---|---|---|
| `project_id` | (required) | GCP project for BigQuery |
| `dataset_id` | `ucp_analytics` | BigQuery dataset name |
| `table_id` | `ucp_events` | BigQuery table name |
| `app_name` | `""` | Tags every event for multi-app filtering |
| `batch_size` | `50` | Events buffered before flush |
| `auto_create_table` | `True` | Create dataset/table on first write |
| `redact_pii` | `False` | Recursively redact configured PII fields in bodies before extraction |
| `pii_fields` | (defaults) | **Override** (not extend) the default redaction set. Passing a list replaces the defaults (`email`, `phone`, `first_name`, `last_name`, `phone_number`, `street_address`, `postal_code`) wholesale — include them in your own list to preserve them. The force-included keys (AP2 credentials, documented PII signals) are always OR'd in afterward regardless of operator config. |
| `custom_metadata` | `None` | Dict attached as JSON to every event |
| `webhook_path_prefixes` | `()` | Additional path prefixes the operator's platform publishes for order webhooks (UCP `order.md`: "The URL format is platform-specific") |
| `jwk_lookup` | `None` | Callable `(keyid: str) -> Optional[Mapping]` used to derive `request_signature_alg` / `response_signature_alg` from JWK `crv` |
| `include_ap2_raw` | `False` | Surface the raw `body.ap2` object into `ap2_mandate_raw_json` after redaction (default-off forensic capture) |
| `include_signals_raw` | `False` | Surface the raw `body.signals` object into `signals_json` after redaction (default-off forensic capture) |

The underlying `AsyncBigQueryWriter` also accepts:

| Parameter | Default | Description |
|---|---|---|
| `max_buffer_size` | `10,000` | Maximum in-memory buffer; oldest events dropped when full |

---

## 12. Relationship to Existing Work

### vs. BigQuery Agent Analytics Plugin (ADK)

| Dimension | BQ Agent Analytics (ADK) | UCP Analytics (this) |
|---|---|---|
| Lives in | `google/adk-python` | `haiyuan-eng-google/data-agent-kit/ucp-analytics/` |
| Hooks into | ADK Runner callbacks | HTTP layer (FastAPI/HTTPX) |
| Understands | Generic agent/tool/model events | UCP checkout, order, payment, capabilities |
| Schema | Flat event rows | Commerce-aware (totals, line items, fulfillment) |
| Correlation | `session_id` | `session_id` + `checkout_session_id` + `order_id` |
| Financial tracking | No | Yes (minor units, currency, per-item) |
| Use together? | Yes | Yes — complementary layers |

The two plugins are complementary: BQ Agent Analytics provides general agent observability (token usage, model calls, tool latency), while UCP Analytics provides commerce-specific metrics (funnel, revenue, payment mix).

---

## 13. Future Work

- **Streaming analytics:** Real-time dashboards via BigQuery BI Engine or Pub/Sub
- **Cost attribution:** Correlate LLM token costs (from ADK plugin) with revenue per checkout session
- **Conformance testing integration:** Validate captured events against UCP conformance test expectations
- **Multi-merchant aggregation:** Cross-merchant funnel analysis for platform operators
- **Embedded Checkout runtime events:** Currently deferred — `ec.totals.change`, link delegation acceptance, reauth, cart binding, and ECP per-shape error variants live in the iframe / host browser as postMessage events. A future slice would add a host-side instrumentation surface (browser SDK + ingest endpoint) so those events can land in the same BigQuery table.
- **Looker Studio template:** Pre-built dashboard deployable via Terraform
