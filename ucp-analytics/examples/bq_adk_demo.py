#!/usr/bin/env python3
"""
ADK + BigQuery Comprehensive Demo — Every Event Type, 3 Transports
======================================================================

Demonstrates the UCPAgentAnalyticsPlugin with simulated ADK tool flows
covering every UCPEventType value. Uses plugin-based tool callbacks for
events the plugin can classify, and a separate UCPAnalyticsTracker for
events it cannot (identity, payment, capability, error, request).

Requires:
    - gcloud auth application-default login
    - BigQuery API enabled
    - pip install ucp-analytics[adk]

Usage:
    uv run python examples/bq_adk_demo.py
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Dict

from _demo_utils import DATASET_ID, PROJECT_ID, TABLE_ID

from ucp_analytics import UCPAnalyticsTracker, UCPEvent, UCPEventType
from ucp_analytics.adk_plugin import UCPAgentAnalyticsPlugin

# ==========================================================================
# Config
# ==========================================================================

APP_NAME = "bq_adk_demo"

ALL_EVENT_TYPES = sorted(e.value for e in UCPEventType)

# ==========================================================================
# Simulated ADK Tool Interface
# ==========================================================================


class MockTool:
    """Simulates an ADK tool object."""

    def __init__(self, name: str):
        self.name = name


class MockToolContext:
    """Simulates an ADK tool context."""

    def __init__(self, app_name: str = APP_NAME):
        self.app_name = app_name
        self._id = id(self)


# ==========================================================================
# Pre-built UCP Response Bodies (spec-aligned)
# ==========================================================================

UCP_VERSION = "2026-01-11"

PAYMENT_HANDLERS = [
    {
        "id": "mock_payment_handler",
        "name": "com.mock.payment",
        "version": UCP_VERSION,
        "spec": "https://ucp.dev/specs/mock",
        "config_schema": "https://ucp.dev/schemas/mock.json",
        "instrument_schemas": [],
        "config": {},
    },
]

SESSION_ID = f"chk_adk_{uuid.uuid4().hex[:8]}"
ORDER_ID = f"order_adk_{uuid.uuid4().hex[:8]}"
CART_ID = f"cart_adk_{uuid.uuid4().hex[:8]}"

DISCOVERY_RESULT = {
    "ucp": {
        "version": UCP_VERSION,
        "services": {
            "dev.ucp.shopping": {
                "version": UCP_VERSION,
                "spec": "https://ucp.dev/specs/shopping",
                "rest": {
                    "schema": "https://ucp.dev/services/shopping/openapi.json",
                    "endpoint": "https://flower-shop.example.com",
                },
            },
        },
        "capabilities": [
            {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
            {
                "name": "dev.ucp.shopping.fulfillment",
                "version": UCP_VERSION,
                "extends": "dev.ucp.shopping.checkout",
            },
        ],
    },
    "payment": {"handlers": PAYMENT_HANDLERS},
}

CHECKOUT_CREATED_RESULT = {
    "ucp": {
        "version": UCP_VERSION,
        "capabilities": [
            {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
        ],
    },
    "id": SESSION_ID,
    "status": "incomplete",
    "currency": "USD",
    "line_items": [
        {
            "id": "li_1",
            "item": {"id": "roses", "title": "Red Roses", "price": 2999},
            "quantity": 1,
        },
    ],
    "totals": [
        {"type": "subtotal", "amount": 2999},
        {"type": "tax", "amount": 262},
        {"type": "total", "amount": 3261},
    ],
    "payment": {
        "handlers": PAYMENT_HANDLERS,
        "instruments": [
            {
                "id": "instr_1",
                "handler_id": "mock_payment_handler",
                "type": "card",
                "brand": "Visa",
            },
        ],
    },
    "messages": [],
    "links": [],
}

CHECKOUT_UPDATED_RESULT = {
    **CHECKOUT_CREATED_RESULT,
    "status": "ready_for_complete",
    "buyer": {"full_name": "Jane Doe", "email": "jane@example.com"},
    "fulfillment": {
        "methods": [
            {
                "id": "method_1",
                "type": "shipping",
                "line_item_ids": ["li_1"],
                "destinations": [
                    {"id": "dest_1", "address_country": "US", "postal_code": "94043"},
                ],
            },
        ],
    },
    "totals": [
        {"type": "subtotal", "amount": 2999},
        {"type": "fulfillment", "amount": 599},
        {"type": "tax", "amount": 262},
        {"type": "total", "amount": 3860},
    ],
}

CHECKOUT_ESCALATION_RESULT = {
    **CHECKOUT_CREATED_RESULT,
    "status": "requires_escalation",
    "continue_url": "https://flower-shop.example.com/checkout/escalate",
    "messages": [
        {
            "type": "error",
            "code": "age_verification_required",
            "content": "Age verification required",
            "severity": "requires_buyer_input",
        },
    ],
}

CHECKOUT_GET_RESULT = {
    **CHECKOUT_UPDATED_RESULT,
}

CHECKOUT_COMPLETED_RESULT = {
    **CHECKOUT_UPDATED_RESULT,
    "status": "completed",
    "order": {
        "id": ORDER_ID,
        "permalink_url": f"https://flower-shop.example.com/orders/{ORDER_ID}",
    },
}

# Second session for cancellation
SESSION2_ID = f"chk_adk2_{uuid.uuid4().hex[:8]}"
CHECKOUT_CANCELED_RESULT = {
    "id": SESSION2_ID,
    "status": "canceled",
    "currency": "USD",
    "totals": [{"type": "total", "amount": 2999}],
}

# Cart results
CART_CREATED_RESULT = {
    "ucp": {"version": UCP_VERSION},
    "id": CART_ID,
    "status": "active",
    "currency": "USD",
    "line_items": [
        {
            "id": "li_c1",
            "item": {"id": "sunflowers", "title": "Sunflower Bunch", "price": 1999},
            "quantity": 2,
        },
    ],
    "totals": [
        {"type": "subtotal", "amount": 3998},
        {"type": "total", "amount": 3998},
    ],
}

CART_UPDATED_RESULT = {
    **CART_CREATED_RESULT,
    "line_items": [
        {
            "id": "li_c1",
            "item": {"id": "sunflowers", "title": "Sunflower Bunch", "price": 1999},
            "quantity": 3,
        },
        {
            "id": "li_c2",
            "item": {"id": "roses", "title": "Red Roses", "price": 2999},
            "quantity": 1,
        },
    ],
    "totals": [
        {"type": "subtotal", "amount": 8996},
        {"type": "total", "amount": 8996},
    ],
}

CART_GET_RESULT = {**CART_UPDATED_RESULT}

CART_CANCELED_RESULT = {**CART_UPDATED_RESULT, "status": "canceled"}

# Order results
ORDER_CREATED_RESULT = {
    "id": ORDER_ID,
    "checkout_id": SESSION_ID,
    "permalink_url": f"https://flower-shop.example.com/orders/{ORDER_ID}",
    "status": "confirmed",
    "line_items": CHECKOUT_CREATED_RESULT["line_items"],
    "totals": CHECKOUT_UPDATED_RESULT["totals"],
    "fulfillment": {
        "expectations": [
            {
                "id": "exp_1",
                "method_type": "shipping",
                "destination": {"address_country": "US", "postal_code": "94043"},
                "line_items": [{"id": "li_1", "quantity": 1}],
            },
        ],
    },
}

ORDER_CONFIRMED_RESULT = {**ORDER_CREATED_RESULT, "status": "confirmed"}

ORDER_SHIPPED_RESULT = {
    **ORDER_CREATED_RESULT,
    "status": "shipped",
    "fulfillment": {
        **ORDER_CREATED_RESULT["fulfillment"],
        "events": [
            {
                "id": "evt_1",
                "type": "shipped",
                "tracking_number": "94001118992234",
                "carrier": "USPS",
                "occurred_at": "2026-02-19T10:00:00Z",
                "line_items": [{"id": "li_1", "quantity": 1}],
            },
        ],
    },
}

ORDER_DELIVERED_RESULT = {**ORDER_CREATED_RESULT, "status": "delivered"}
ORDER_RETURNED_RESULT = {**ORDER_CREATED_RESULT, "status": "returned"}
ORDER_CANCELED_RESULT = {**ORDER_CREATED_RESULT, "status": "canceled"}

# Order body for a PUT (REST mutation) — keep `status: confirmed`
# so the lifecycle helper returns None and the classifier falls
# through to ORDER_UPDATED.
ORDER_UPDATED_RESULT = {**ORDER_CREATED_RESULT, "label": "ORD-2026-00042"}

# Webhook delivery without recognizable lifecycle — no
# fulfillment.events, no adjustments, no legacy status. The
# classifier enters the webhook branch (path matches /webhook(s))
# and falls all the way through to ORDER_WEBHOOK_RECEIVED.
ORDER_WEBHOOK_RECEIVED_RESULT = {
    "id": ORDER_ID,
    "checkout_id": SESSION_ID,
}

# Catalog results — shape-only fixtures, no PII.
CATALOG_SEARCH_RESULT = {
    "products": [
        {"id": "bouquet_roses", "title": "Rose Bouquet", "price": 2999},
    ],
    "query": "roses",
}
CATALOG_LOOKUP_RESULT = {
    "products": [
        {"id": "bouquet_roses", "title": "Rose Bouquet", "price": 2999},
        {"id": "sunflower_bunch", "title": "Sunflower Bunch", "price": 1999},
    ],
}
CATALOG_PRODUCT_RESULT = {
    "id": "bouquet_roses",
    "title": "Rose Bouquet",
    "price": 2999,
    "variants": [],
}


# ==========================================================================
# Simulate ADK tool call
# ==========================================================================


async def simulate_tool_call(
    plugin: UCPAgentAnalyticsPlugin,
    tool_name: str,
    tool_args: dict,
    result: dict,
    delay_ms: float = 50,
):
    """Simulate before_tool -> delay -> after_tool ADK callback flow."""
    tool = MockTool(tool_name)
    ctx = MockToolContext()

    await plugin.before_tool_callback(
        tool=tool,
        tool_args=tool_args,
        tool_context=ctx,
    )

    await asyncio.sleep(delay_ms / 1000)

    await plugin.after_tool_callback(
        tool=tool,
        tool_args=tool_args,
        tool_context=ctx,
        result=result,
    )

    return result


# ==========================================================================
# Phase 1: Plugin-based tool calls (22 event types via _TOOL_TO_HTTP)
# ==========================================================================


async def run_plugin_phase(plugin: UCPAgentAnalyticsPlugin):
    """Run all plugin-classifiable tool calls."""
    print("\n" + "=" * 70)
    print("  PHASE 1: ADK Plugin Tool Calls (22 event types)")
    print("=" * 70)

    steps = [
        # (tool_name, args, result, label)
        (
            "discover_merchant",
            {},
            DISCOVERY_RESULT,
            "profile_discovered",
        ),
        (
            "catalog_search",
            {"query": "roses"},
            CATALOG_SEARCH_RESULT,
            "catalog_search",
        ),
        (
            "catalog_lookup",
            {"ids": ["bouquet_roses", "sunflower_bunch"]},
            CATALOG_LOOKUP_RESULT,
            "catalog_lookup",
        ),
        (
            "get_product",
            {"id": "bouquet_roses"},
            CATALOG_PRODUCT_RESULT,
            "catalog_product_get",
        ),
        (
            "create_checkout",
            {"line_items": [{"item_id": "roses", "quantity": 1}]},
            CHECKOUT_CREATED_RESULT,
            "checkout_session_created",
        ),
        (
            "update_checkout",
            {"session_id": SESSION_ID, "buyer": {"email": "j@e.com"}},
            CHECKOUT_UPDATED_RESULT,
            "checkout_session_updated",
        ),
        (
            "update_checkout",
            {"session_id": SESSION_ID},
            CHECKOUT_ESCALATION_RESULT,
            "checkout_escalation",
        ),
        (
            "get_checkout",
            {"session_id": SESSION_ID},
            CHECKOUT_GET_RESULT,
            "checkout_session_get",
        ),
        (
            "complete_checkout",
            {"session_id": SESSION_ID, "payment_instrument": "instr_1"},
            CHECKOUT_COMPLETED_RESULT,
            "checkout_session_completed",
        ),
        (
            "cancel_checkout",
            {"session_id": SESSION2_ID},
            CHECKOUT_CANCELED_RESULT,
            "checkout_session_canceled",
        ),
        (
            "create_cart",
            {"line_items": [{"item_id": "sunflowers", "quantity": 2}]},
            CART_CREATED_RESULT,
            "cart_created",
        ),
        (
            "update_cart",
            {
                "cart_id": CART_ID,
                "line_items": [
                    {"item_id": "sunflowers", "quantity": 3},
                ],
            },
            CART_UPDATED_RESULT,
            "cart_updated",
        ),
        (
            "get_cart",
            {"cart_id": CART_ID},
            CART_GET_RESULT,
            "cart_get",
        ),
        (
            "cancel_cart",
            {"cart_id": CART_ID},
            CART_CANCELED_RESULT,
            "cart_canceled",
        ),
        (
            "create_order",
            {"checkout_id": SESSION_ID},
            ORDER_CREATED_RESULT,
            "order_created",
        ),
        (
            # GET /orders/{id} with a body that has no recognizable
            # lifecycle status (legacy `confirmed` isn't mapped by
            # the c5c6139 lifecycle helper) -> order_get under the
            # new B5 / B8 taxonomy.
            "get_order",
            {"order_id": ORDER_ID},
            ORDER_CONFIRMED_RESULT,
            "order_get",
        ),
        (
            "simulate_shipping",
            {"order_id": ORDER_ID},
            ORDER_SHIPPED_RESULT,
            "order_shipped",
        ),
        (
            "get_order",
            {"order_id": ORDER_ID},
            ORDER_DELIVERED_RESULT,
            "order_delivered",
        ),
        (
            "get_order",
            {"order_id": ORDER_ID},
            ORDER_RETURNED_RESULT,
            "order_returned",
        ),
        (
            "get_order",
            {"order_id": ORDER_ID},
            ORDER_CANCELED_RESULT,
            "order_canceled",
        ),
        (
            # PUT /orders/{id} with a confirmed (no-lifecycle) body →
            # falls through to ORDER_UPDATED (the REST-mutation type,
            # distinct from order_get and order_webhook_received).
            "update_order",
            {"order_id": ORDER_ID, "label": "ORD-2026-00042"},
            ORDER_UPDATED_RESULT,
            "order_updated",
        ),
        (
            # POST /webhooks/partners/{id}/events/order with a body
            # carrying no recognizable lifecycle → ORDER_WEBHOOK_RECEIVED
            # (B5b: don't pivot taxonomy on URL format).
            "order_event_webhook",
            {"order_id": ORDER_ID},
            ORDER_WEBHOOK_RECEIVED_RESULT,
            "order_webhook_received",
        ),
    ]

    for i, (tool_name, args, result, expected_type) in enumerate(steps, 1):
        await simulate_tool_call(plugin, tool_name, args, result, delay_ms=30 + i * 5)
        print(f"   {i:>2}. {tool_name:<25} -> {expected_type}")


# ==========================================================================
# Phase 2: Direct tracker events (10 event types not in plugin)
# ==========================================================================


async def run_direct_events(tracker: UCPAnalyticsTracker):
    """Record events the plugin cannot classify."""
    print("\n" + "=" * 70)
    print("  PHASE 2: Direct Tracker Events (10 event types)")
    print("=" * 70)

    direct_events = [
        # Identity events
        UCPEvent(
            event_type="identity_link_initiated",
            app_name=APP_NAME,
            identity_provider="google",
            identity_scope="profile email",
            latency_ms=20.0,
        ),
        UCPEvent(
            event_type="identity_link_completed",
            app_name=APP_NAME,
            identity_provider="google",
            identity_scope="profile email",
            latency_ms=15.0,
        ),
        UCPEvent(
            event_type="identity_link_revoked",
            app_name=APP_NAME,
            identity_provider="google",
            latency_ms=10.0,
        ),
        # Payment events
        UCPEvent(
            event_type="payment_handler_negotiated",
            app_name=APP_NAME,
            checkout_session_id=SESSION_ID,
            payment_handler_id="com.mock.payment",
            currency="USD",
            total_amount=3860,
            latency_ms=12.0,
        ),
        UCPEvent(
            event_type="payment_instrument_selected",
            app_name=APP_NAME,
            checkout_session_id=SESSION_ID,
            payment_handler_id="com.mock.payment",
            payment_instrument_type="card",
            payment_brand="Visa",
            currency="USD",
            total_amount=3860,
            latency_ms=8.0,
        ),
        UCPEvent(
            event_type="payment_completed",
            app_name=APP_NAME,
            checkout_session_id=SESSION_ID,
            payment_handler_id="com.mock.payment",
            payment_instrument_type="card",
            payment_brand="Visa",
            currency="USD",
            total_amount=3860,
            latency_ms=45.0,
        ),
        UCPEvent(
            event_type="payment_failed",
            app_name=APP_NAME,
            checkout_session_id=SESSION_ID,
            payment_handler_id="com.mock.payment",
            payment_instrument_type="card",
            error_code="card_declined",
            error_message="Insufficient funds",
            currency="USD",
            total_amount=3860,
            latency_ms=30.0,
        ),
        # Capability negotiation
        UCPEvent(
            event_type="capability_negotiated",
            app_name=APP_NAME,
            ucp_version=UCP_VERSION,
            capabilities_json=json.dumps(
                [
                    {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
                    {"name": "dev.ucp.shopping.fulfillment", "version": UCP_VERSION},
                ]
            ),
            latency_ms=10.0,
        ),
        # Error event
        UCPEvent(
            event_type="error",
            app_name=APP_NAME,
            http_status_code=404,
            error_code="not_found",
            error_message="Checkout session not found",
            latency_ms=5.0,
        ),
        # Fallback request event
        UCPEvent(
            event_type="request",
            app_name=APP_NAME,
            http_method="POST",
            http_path="/some-unknown-endpoint",
            http_status_code=200,
            latency_ms=3.0,
        ),
    ]

    for event in direct_events:
        await tracker.record_event(event)
        print(f"   Recorded: {event.event_type}")


# ==========================================================================
# Phase 3: MCP transport replay
# ==========================================================================


async def run_mcp_transport(tracker: UCPAnalyticsTracker):
    """Replay key operations via MCP transport."""
    print("\n" + "=" * 70)
    print("  PHASE 3: MCP Transport — JSON-RPC Replay")
    print("=" * 70)

    mcp_calls = [
        ("discover_merchant", DISCOVERY_RESULT),
        ("create_checkout", CHECKOUT_CREATED_RESULT),
        ("update_checkout", CHECKOUT_UPDATED_RESULT),
        ("complete_checkout", CHECKOUT_COMPLETED_RESULT),
        ("create_cart", CART_CREATED_RESULT),
        ("get_order", ORDER_CONFIRMED_RESULT),
        (
            "link_identity",
            {
                "provider": "google",
                "scope": "profile email",
                "status": "pending",
            },
        ),
        ("revoke_identity", {"status": "revoked"}),
        (
            "negotiate_capability",
            {
                "ucp": {"version": UCP_VERSION},
            },
        ),
    ]

    for tool_name, body in mcp_calls:
        event = await tracker.record_jsonrpc(
            tool_name=tool_name,
            transport="mcp",
            status_code=200,
            response_body=body,
            latency_ms=25.0,
            merchant_host="flower-shop.example.com",
        )
        print(f"   MCP: {tool_name:<30} -> {event.event_type}")


# ==========================================================================
# Phase 4: A2A transport replay
# ==========================================================================


async def run_a2a_transport(tracker: UCPAnalyticsTracker):
    """Replay key operations via A2A transport."""
    print("\n" + "=" * 70)
    print("  PHASE 4: A2A Transport — JSON-RPC Replay")
    print("=" * 70)

    a2a_calls = [
        ("a2a.ucp.discover", DISCOVERY_RESULT),
        ("a2a.ucp.checkout.create", CHECKOUT_CREATED_RESULT),
        ("a2a.ucp.checkout.update", CHECKOUT_UPDATED_RESULT),
        ("a2a.ucp.checkout.complete", CHECKOUT_COMPLETED_RESULT),
        ("a2a.ucp.cart.create", CART_CREATED_RESULT),
        ("a2a.ucp.order.get", ORDER_CONFIRMED_RESULT),
        (
            "a2a.ucp.identity.link",
            {
                "provider": "google",
                "scope": "profile email",
                "status": "pending",
            },
        ),
        ("a2a.ucp.identity.revoke", {"status": "revoked"}),
        (
            "a2a.ucp.capability.negotiate",
            {
                "ucp": {"version": UCP_VERSION},
            },
        ),
    ]

    for tool_name, body in a2a_calls:
        event = await tracker.record_jsonrpc(
            tool_name=tool_name,
            transport="a2a",
            status_code=200,
            response_body=body,
            latency_ms=30.0,
            merchant_host="flower-shop.example.com",
        )
        print(f"   A2A: {tool_name:<35} -> {event.event_type}")


# ==========================================================================
# Phase 5: Non-UCP tool (should be skipped by plugin)
# ==========================================================================


async def run_non_ucp_tool(plugin: UCPAgentAnalyticsPlugin):
    """Verify non-UCP tools are skipped."""
    print("\n" + "=" * 70)
    print("  PHASE 5: Non-UCP Tool (should be skipped)")
    print("=" * 70)

    await simulate_tool_call(
        plugin,
        "get_weather",
        {"location": "San Francisco"},
        {"temperature": 68, "condition": "sunny"},
        delay_ms=20,
    )
    print("   get_weather -> skipped (not a UCP tool)")


# ==========================================================================
# BigQuery Verification
# ==========================================================================


async def verify_bigquery():
    from google.cloud import bigquery

    print("\n" + "=" * 70)
    print("  BIGQUERY VERIFICATION — Every Event Type")
    print("=" * 70)

    client = bigquery.Client(project=PROJECT_ID)
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"

    print("\n   Waiting 10s for BigQuery streaming buffer...")
    await asyncio.sleep(10)

    query = f"""
    SELECT event_type, transport, COUNT(*) as cnt
    FROM `{table_ref}`
    WHERE app_name = '{APP_NAME}'
    GROUP BY event_type, transport
    ORDER BY event_type, transport
    """
    print("   Querying BigQuery...\n")

    rows = list(client.query(query).result())

    if not rows:
        print("   WARNING: No rows found in BigQuery yet.")
        print("   Streaming inserts may take up to 90 seconds to be queryable.")
        print(
            f"   Run manually: SELECT * FROM `{table_ref}`"
            f" WHERE app_name = '{APP_NAME}'"
        )
        client.close()
        return

    # Build event type -> transport counts
    event_map: Dict[str, Dict[str, int]] = {}
    for row in rows:
        et = row.event_type
        tp = row.transport or "rest"
        if et not in event_map:
            event_map[et] = {}
        event_map[et][tp] = row.cnt

    # Print summary table
    print(f"   {'Event Type':<35} {'REST':>5} {'MCP':>5} {'A2A':>5} {'Total':>6}")
    print("   " + "-" * 60)

    total_events = 0
    for et in sorted(event_map.keys()):
        rest_cnt = event_map[et].get("rest", 0)
        mcp_cnt = event_map[et].get("mcp", 0)
        a2a_cnt = event_map[et].get("a2a", 0)
        row_total = rest_cnt + mcp_cnt + a2a_cnt
        total_events += row_total
        print(f"   {et:<35} {rest_cnt:>5} {mcp_cnt:>5} {a2a_cnt:>5} {row_total:>6}")

    print("   " + "-" * 60)
    print(f"   {'TOTAL':<35} {'':>5} {'':>5} {'':>5} {total_events:>6}")

    # Verification checks
    found_types = set(event_map.keys())
    expected_types = set(ALL_EVENT_TYPES)
    missing = expected_types - found_types
    extra = found_types - expected_types

    print(f"\n   Event types found: {len(found_types)}/{len(ALL_EVENT_TYPES)}")

    print("\n   Verification:")
    print(f"     [{'PASS' if not missing else 'FAIL'}] Every event type present")
    if missing:
        print(f"       Missing: {sorted(missing)}")
    if extra:
        print(f"       Extra: {sorted(extra)}")

    # Check transports
    has_rest = any("rest" in v for v in event_map.values())
    has_mcp = any("mcp" in v for v in event_map.values())
    has_a2a = any("a2a" in v for v in event_map.values())
    print(f"     [{'PASS' if has_rest else 'FAIL'}] REST transport events present")
    print(f"     [{'PASS' if has_mcp else 'FAIL'}] MCP transport events present")
    print(f"     [{'PASS' if has_a2a else 'FAIL'}] A2A transport events present")

    # Check non-UCP tool was skipped
    has_weather = any("weather" in et for et in found_types)
    skip_ok = "PASS" if not has_weather else "FAIL"
    print(f"     [{skip_ok}] Non-UCP tool correctly skipped")

    # Check latency
    latency_query = f"""
    SELECT COUNT(*) as cnt
    FROM `{table_ref}`
    WHERE app_name = '{APP_NAME}' AND latency_ms > 0
    """
    latency_rows = list(client.query(latency_query).result())
    has_latency = latency_rows[0].cnt > 0 if latency_rows else False
    lat_ok = "PASS" if has_latency else "FAIL"
    print(f"     [{lat_ok}] Latency captured from tool timing")

    all_ok = not missing and has_rest and has_mcp and has_a2a and not has_weather
    if all_ok:
        print("\n   All ADK BigQuery verifications passed!")
    else:
        print("\n   Some verifications failed — check streaming buffer delay.")

    client.close()


# ==========================================================================
# Main
# ==========================================================================


async def main():
    print("\n" + "=" * 70)
    print("  UCP ANALYTICS — ADK Plugin Comprehensive BigQuery Demo")
    print("=" * 70)

    # Create ADK plugin (writes to BigQuery)
    plugin = UCPAgentAnalyticsPlugin(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        table_id=TABLE_ID,
        app_name=APP_NAME,
        batch_size=1,
        track_all_tools=False,
    )

    # Separate tracker for events the plugin can't handle
    tracker = UCPAnalyticsTracker(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        table_id=TABLE_ID,
        app_name=APP_NAME,
        batch_size=1,
        auto_create_table=True,
    )

    # Phase 1: Plugin-based tool calls (22 event types)
    await run_plugin_phase(plugin)

    # Phase 2: Direct tracker events (10 event types)
    await run_direct_events(tracker)

    # Phase 3: MCP transport replay
    await run_mcp_transport(tracker)

    # Phase 4: A2A transport replay
    await run_a2a_transport(tracker)

    # Phase 5: Non-UCP tool (should be skipped)
    await run_non_ucp_tool(plugin)

    # Flush and close
    print("\n   Flushing events to BigQuery...")
    await plugin.close()
    await tracker.close()

    # BigQuery verification
    await verify_bigquery()
    print("\n   Done!")


if __name__ == "__main__":
    asyncio.run(main())
