#!/usr/bin/env python3
"""
BigQuery Comprehensive Demo — Every Event Type, 3 Transports
================================================================

Runs a mini UCP merchant server + shopping agent client. Exercises every
UCPEventType value across REST, MCP, and A2A transports. Uses UCP Python
SDK Pydantic models and verifies all events in BigQuery.

Requires:
    - gcloud auth application-default login (or service account)
    - BigQuery API enabled

Usage:
    uv run python examples/bq_demo.py
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Dict

import httpx
import uvicorn
from _demo_utils import DATASET_ID, PROJECT_ID, TABLE_ID
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ucp_analytics import (
    UCPAnalyticsMiddleware,
    UCPAnalyticsTracker,
    UCPClientEventHook,
    UCPEvent,
    UCPEventType,
)

# ==========================================================================
# Config — set GCP_PROJECT_ID env var, or edit the fallback below.
# ==========================================================================

APP_NAME = "bq_demo"

# ==========================================================================
# Mini UCP Merchant Server — all endpoints
# ==========================================================================

SESSIONS: Dict[str, dict] = {}
CARTS: Dict[str, dict] = {}
ORDERS: Dict[str, dict] = {}
IDENTITIES: Dict[str, dict] = {}
UCP_VERSION = "2026-01-11"

PRODUCTS = {
    "bouquet_roses": {"title": "Red Rose Bouquet", "price": 2999},
    "sunflower_bunch": {"title": "Sunflower Bunch", "price": 1999},
}

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

server_tracker: UCPAnalyticsTracker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if server_tracker:
        await server_tracker.close()


app = FastAPI(title="UCP Flower Shop (BQ Comprehensive Demo)", lifespan=lifespan)


# --- Discovery ---
@app.get("/.well-known/ucp")
async def discovery():
    return {
        "ucp": {
            "version": UCP_VERSION,
            "services": {
                "dev.ucp.shopping": {
                    "version": UCP_VERSION,
                    "spec": "https://ucp.dev/specs/shopping",
                    "rest": {
                        "schema": "https://ucp.dev/services/shopping/openapi.json",
                        "endpoint": "http://localhost:8199",
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
                {
                    "name": "dev.ucp.shopping.discount",
                    "version": UCP_VERSION,
                    "extends": "dev.ucp.shopping.checkout",
                },
            ],
        },
        "payment": {"handlers": PAYMENT_HANDLERS},
    }


# --- Checkout endpoints ---
@app.post("/checkout-sessions")
async def create_checkout(request: Request):
    body = await request.json()
    session_id = f"chk_{uuid.uuid4().hex[:12]}"

    line_items = []
    subtotal = 0
    for item in body.get("line_items", []):
        product_id = item.get("item", {}).get("id", "")
        product = PRODUCTS.get(product_id, {"title": product_id, "price": 0})
        qty = item.get("quantity", 1)
        li = {
            "id": f"li_{uuid.uuid4().hex[:8]}",
            "item": {
                "id": product_id,
                "title": product["title"],
                "price": product["price"],
            },
            "quantity": qty,
        }
        line_items.append(li)
        subtotal += product["price"] * qty

    tax = round(subtotal * 0.0875)
    total = subtotal + tax

    session = {
        "ucp": {
            "version": UCP_VERSION,
            "capabilities": [
                {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
                {"name": "dev.ucp.shopping.fulfillment", "version": UCP_VERSION},
            ],
        },
        "id": session_id,
        "status": "incomplete",
        "currency": body.get("currency", "USD"),
        "line_items": line_items,
        "totals": [
            {"type": "subtotal", "amount": subtotal},
            {"type": "tax", "amount": tax},
            {"type": "total", "amount": total},
        ],
        "buyer": body.get("buyer", {}),
        "payment": {
            "handlers": PAYMENT_HANDLERS,
            "instruments": [
                {
                    "id": "instr_card",
                    "handler_id": "mock_payment_handler",
                    "type": "card",
                    "brand": "Visa",
                },
            ],
        },
        "messages": [],
        "links": [],
    }
    SESSIONS[session_id] = session
    return JSONResponse(session, status_code=201)


@app.put("/checkout-sessions/{session_id}")
async def update_checkout(session_id: str, request: Request):
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)

    body = await request.json()
    session = SESSIONS[session_id]

    if "buyer" in body:
        session["buyer"] = body["buyer"]

    if "fulfillment" in body:
        session["fulfillment"] = body["fulfillment"]
        shipping = 599
        keep = ("fulfillment", "total")
        session["totals"] = [t for t in session["totals"] if t["type"] not in keep]
        subtotal = next(
            t["amount"] for t in session["totals"] if t["type"] == "subtotal"
        )
        tax = next(t["amount"] for t in session["totals"] if t["type"] == "tax")
        session["totals"].append({"type": "fulfillment", "amount": shipping})
        session["totals"].append({"type": "total", "amount": subtotal + tax + shipping})

    # Support triggering escalation via a special flag
    if body.get("_force_escalation"):
        session["status"] = "requires_escalation"
        session["continue_url"] = "https://shop.example.com/checkout/escalate"
        session["messages"] = [
            {
                "type": "error",
                "code": "age_verification_required",
                "content": "Age verification required for this product",
                "severity": "requires_buyer_input",
            },
        ]
        SESSIONS[session_id] = session
        return JSONResponse(session)

    has_buyer = bool(session.get("buyer", {}).get("email"))
    has_fulfillment = "fulfillment" in session
    if has_buyer and has_fulfillment:
        session["status"] = "ready_for_complete"
    else:
        session["status"] = "incomplete"

    SESSIONS[session_id] = session
    return JSONResponse(session)


@app.get("/checkout-sessions/{session_id}")
async def get_checkout(session_id: str):
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(SESSIONS[session_id])


@app.post("/checkout-sessions/{session_id}/complete")
async def complete_checkout(session_id: str, request: Request):
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)

    body = await request.json()
    session = SESSIONS[session_id]
    payment = body.get("payment", {})

    if not payment.get("instruments"):
        session["status"] = "requires_escalation"
        session["continue_url"] = "https://shop.example.com/checkout/escalate"
        session["messages"] = [
            {
                "type": "error",
                "code": "missing_payment",
                "content": "Payment instrument required",
                "severity": "requires_buyer_input",
            },
        ]
        SESSIONS[session_id] = session
        return JSONResponse(session, status_code=400)

    order_id = f"order_{uuid.uuid4().hex[:10]}"
    session["status"] = "completed"
    session["order"] = {
        "id": order_id,
        "permalink_url": f"https://shop.example.com/orders/{order_id}",
    }

    ORDERS[order_id] = {
        "id": order_id,
        "checkout_id": session_id,
        "permalink_url": f"https://shop.example.com/orders/{order_id}",
        "status": "confirmed",
        "line_items": session["line_items"],
        "totals": session["totals"],
        "fulfillment": {
            "expectations": [
                {
                    "id": "exp_1",
                    "method_type": "shipping",
                    "destination": {"address_country": "US", "postal_code": "94043"},
                    "line_items": [
                        {"id": li["id"], "quantity": li["quantity"]}
                        for li in session["line_items"]
                    ],
                    "description": "Standard Shipping",
                },
            ],
        },
    }

    SESSIONS[session_id] = session
    return JSONResponse(session)


@app.post("/checkout-sessions/{session_id}/cancel")
async def cancel_checkout(session_id: str):
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    session = SESSIONS[session_id]
    session["status"] = "canceled"
    SESSIONS[session_id] = session
    return JSONResponse(session)


# --- Cart endpoints ---
@app.post("/carts")
async def create_cart(request: Request):
    body = await request.json()
    cart_id = f"cart_{uuid.uuid4().hex[:12]}"

    line_items = []
    subtotal = 0
    for item in body.get("line_items", []):
        product_id = item.get("item", {}).get("id", "")
        product = PRODUCTS.get(product_id, {"title": product_id, "price": 0})
        qty = item.get("quantity", 1)
        li = {
            "id": f"li_{uuid.uuid4().hex[:8]}",
            "item": {
                "id": product_id,
                "title": product["title"],
                "price": product["price"],
            },
            "quantity": qty,
        }
        line_items.append(li)
        subtotal += product["price"] * qty

    cart = {
        "ucp": {"version": UCP_VERSION},
        "id": cart_id,
        "status": "active",
        "currency": body.get("currency", "USD"),
        "line_items": line_items,
        "totals": [
            {"type": "subtotal", "amount": subtotal},
            {"type": "total", "amount": subtotal},
        ],
    }
    CARTS[cart_id] = cart
    return JSONResponse(cart, status_code=201)


@app.put("/carts/{cart_id}")
async def update_cart(cart_id: str, request: Request):
    if cart_id not in CARTS:
        return JSONResponse({"error": "not_found"}, status_code=404)

    body = await request.json()
    cart = CARTS[cart_id]

    if "line_items" in body:
        new_items = []
        subtotal = 0
        for item in body["line_items"]:
            product_id = item.get("item", {}).get("id", "")
            product = PRODUCTS.get(product_id, {"title": product_id, "price": 0})
            qty = item.get("quantity", 1)
            li = {
                "id": f"li_{uuid.uuid4().hex[:8]}",
                "item": {
                    "id": product_id,
                    "title": product["title"],
                    "price": product["price"],
                },
                "quantity": qty,
            }
            new_items.append(li)
            subtotal += product["price"] * qty
        cart["line_items"] = new_items
        cart["totals"] = [
            {"type": "subtotal", "amount": subtotal},
            {"type": "total", "amount": subtotal},
        ]

    CARTS[cart_id] = cart
    return JSONResponse(cart)


@app.get("/carts/{cart_id}")
async def get_cart(cart_id: str):
    if cart_id not in CARTS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(CARTS[cart_id])


@app.post("/carts/{cart_id}/cancel")
async def cancel_cart(cart_id: str):
    if cart_id not in CARTS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    cart = CARTS[cart_id]
    cart["status"] = "canceled"
    CARTS[cart_id] = cart
    return JSONResponse(cart)


# --- Catalog endpoints ---
@app.post("/catalog/search")
async def catalog_search(request: Request):
    body = await request.json()
    return JSONResponse(
        {
            "products": [
                {"id": "sku_rose", "title": "Rose Bouquet", "price": 2999},
                {"id": "sku_lily", "title": "Lily Bouquet", "price": 3499},
            ],
            "query": body.get("query", ""),
        }
    )


@app.post("/catalog/lookup")
async def catalog_lookup(request: Request):
    body = await request.json()
    return JSONResponse(
        {
            "products": [
                {"id": pid, "title": f"Product {pid}", "price": 1999}
                for pid in body.get("ids", [])
            ],
        }
    )


@app.post("/catalog/product")
async def catalog_product(request: Request):
    body = await request.json()
    return JSONResponse(
        {
            "id": body.get("id", "sku_rose"),
            "title": "Rose Bouquet",
            "price": 2999,
            "variants": [],
        }
    )


# --- Webhook endpoint (order events) ---
@app.post("/webhooks/orders")
async def webhook_orders(request: Request):
    """Operator-specific webhook URL the platform posts order events to."""
    return JSONResponse({"status": "ok"})


# --- Order endpoints ---
@app.post("/orders")
async def create_order(request: Request):
    body = await request.json()
    order_id = f"order_{uuid.uuid4().hex[:10]}"
    order = {
        "id": order_id,
        "checkout_id": body.get("checkout_id", ""),
        "permalink_url": f"https://shop.example.com/orders/{order_id}",
        "status": "confirmed",
        "line_items": body.get("line_items", []),
        "totals": body.get("totals", []),
        "fulfillment": {
            "expectations": [
                {
                    "id": "exp_1",
                    "method_type": "shipping",
                    "destination": {"address_country": "US", "postal_code": "94043"},
                    "line_items": [],
                    "description": "Standard Shipping",
                },
            ],
        },
    }
    ORDERS[order_id] = order
    return JSONResponse(order, status_code=201)


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(ORDERS[order_id])


@app.put("/orders/{order_id}")
async def update_order(order_id: str, request: Request):
    """REST-driven order mutation — classifies as order_updated."""
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    body = await request.json()
    # Allow label updates; leave lifecycle state alone (otherwise
    # the lifecycle wins on the classifier).
    if "label" in body:
        ORDERS[order_id]["label"] = body["label"]
    return JSONResponse(ORDERS[order_id])


def _append_fulfillment_event(order_id: str, event_type: str) -> None:
    """Append a c5c6139-shape fulfillment event with a fresh
    occurred_at, so the lifecycle classifier picks the latest entry."""
    from datetime import datetime, timezone

    ORDERS[order_id]["fulfillment"].setdefault("events", []).append(
        {
            "id": f"fe_{uuid.uuid4().hex[:6]}",
            "occurred_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "type": event_type,
            "line_items": [],
        }
    )
    # Keep legacy `status` field in sync for older dashboards.
    ORDERS[order_id]["status"] = event_type


@app.post("/orders/{order_id}/deliver")
async def deliver_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    _append_fulfillment_event(order_id, "delivered")
    return JSONResponse(ORDERS[order_id])


@app.post("/orders/{order_id}/return")
async def return_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    _append_fulfillment_event(order_id, "returned_to_sender")
    return JSONResponse(ORDERS[order_id])


@app.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    _append_fulfillment_event(order_id, "canceled")
    return JSONResponse(ORDERS[order_id])


@app.post("/testing/simulate-shipping/{order_id}")
async def simulate_shipping(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    _append_fulfillment_event(order_id, "shipped")
    ORDERS[order_id]["fulfillment"]["events"][-1].update(
        {
            "tracking_number": "9400111899223456789012",
            "carrier": "USPS",
        }
    )
    return JSONResponse(ORDERS[order_id])


# --- Identity endpoints ---
@app.post("/identity")
async def identity_link(request: Request):
    body = await request.json()
    link_id = f"idl_{uuid.uuid4().hex[:8]}"
    IDENTITIES[link_id] = {
        "id": link_id,
        "provider": body.get("provider", "google"),
        "scope": body.get("scope", "profile email"),
        "status": "pending",
        "redirect_url": f"https://accounts.google.com/o/oauth2/auth?state={link_id}",
    }
    return JSONResponse(IDENTITIES[link_id], status_code=201)


@app.get("/identity/callback")
async def identity_callback(request: Request):
    link_id = request.query_params.get("state", "")
    identity = IDENTITIES.get(link_id, {})
    identity["status"] = "linked"
    return JSONResponse(
        {
            "identity": {
                "provider": identity.get("provider", "google"),
                "scope": identity.get("scope", "profile email"),
            },
            "status": "linked",
        }
    )


@app.post("/identity/revoke")
async def identity_revoke(request: Request):
    body = await request.json()
    link_id = body.get("link_id", "")
    if link_id in IDENTITIES:
        IDENTITIES[link_id]["status"] = "revoked"
    return JSONResponse({"status": "revoked"})


# --- Fallback endpoint for unmatched paths (200) ---
@app.api_route("/some-unmatched-endpoint", methods=["POST"])
async def unmatched_endpoint(request: Request):
    return JSONResponse({"status": "ok"})


# ==========================================================================
# Shopping Agent — REST transport (every event type)
# ==========================================================================

ALL_EVENT_TYPES = sorted(e.value for e in UCPEventType)


async def run_rest_flow(client_tracker: UCPAnalyticsTracker) -> tuple[str, str]:
    """Run the full REST flow generating events for every UCPEventType."""
    print("\n" + "=" * 70)
    print("  PHASE 1: REST Transport — Full Event Coverage")
    print("=" * 70)

    hook = UCPClientEventHook(client_tracker)

    async with httpx.AsyncClient(
        base_url="http://localhost:8199",
        event_hooks={"response": [hook]},
        headers={
            "UCP-Agent": 'profile="https://agent.example.com/profile"',
            "Content-Type": "application/json",
        },
    ) as client:
        # 1. Discovery -> profile_discovered
        print("\n-- 1. Discovery (profile_discovered) --")
        resp = await client.get("/.well-known/ucp")
        profile = resp.json()
        print(f"   UCP version: {profile['ucp']['version']}")

        # 1a. Catalog Search -> catalog_search
        print("\n-- 1a. Catalog Search (catalog_search) --")
        resp = await client.post("/catalog/search", json={"query": "roses"})
        print(f"   Found: {len(resp.json().get('products', []))} products")

        # 1b. Catalog Lookup -> catalog_lookup
        print("\n-- 1b. Catalog Lookup (catalog_lookup) --")
        resp = await client.post(
            "/catalog/lookup",
            json={"ids": ["bouquet_roses", "sunflower_bunch"]},
        )
        print(f"   Looked up: {len(resp.json().get('products', []))} products")

        # 1c. Catalog Product Get -> catalog_product_get
        print("\n-- 1c. Catalog Product Get (catalog_product_get) --")
        resp = await client.post("/catalog/product", json={"id": "bouquet_roses"})
        print(f"   Product: {resp.json().get('title')}")

        # 2. Create Checkout -> checkout_session_created
        print("\n-- 2. Create Checkout (checkout_session_created) --")
        resp = await client.post(
            "/checkout-sessions",
            json={
                "line_items": [
                    {"item": {"id": "bouquet_roses"}, "quantity": 2},
                    {"item": {"id": "sunflower_bunch"}, "quantity": 1},
                ],
                "buyer": {"full_name": "Jane Doe"},
                "currency": "USD",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        checkout = resp.json()
        session_id = checkout["id"]
        print(f"   Session: {session_id}, Status: {checkout['status']}")

        # 3. Update Checkout -> checkout_session_updated
        print("\n-- 3. Update Checkout (checkout_session_updated) --")
        resp = await client.put(
            f"/checkout-sessions/{session_id}",
            json={
                "buyer": {"full_name": "Jane Doe", "email": "jane@example.com"},
                "fulfillment": {
                    "methods": [
                        {
                            "id": "method_1",
                            "type": "shipping",
                            "line_item_ids": [],
                            "destinations": [
                                {
                                    "id": "dest_1",
                                    "address_country": "US",
                                    "postal_code": "94043",
                                    "address_locality": "Mountain View",
                                    "address_region": "CA",
                                },
                            ],
                        }
                    ]
                },
            },
        )
        updated = resp.json()
        print(f"   Status: {updated['status']}")

        # 4. Get Checkout -> checkout_session_get
        print("\n-- 4. Get Checkout (checkout_session_get) --")
        resp = await client.get(f"/checkout-sessions/{session_id}")
        print(f"   Status: {resp.json()['status']}")

        # 5. Trigger Escalation -> checkout_escalation
        print("\n-- 5. Escalation (checkout_escalation) --")
        resp = await client.put(
            f"/checkout-sessions/{session_id}",
            json={"_force_escalation": True},
        )
        escalated = resp.json()
        print(f"   Status: {escalated['status']}")

        # Reset status back to ready for completion
        resp = await client.put(
            f"/checkout-sessions/{session_id}",
            json={
                "buyer": {"full_name": "Jane Doe", "email": "jane@example.com"},
                "fulfillment": {
                    "methods": [
                        {
                            "id": "method_1",
                            "type": "shipping",
                            "line_item_ids": [],
                            "destinations": [
                                {
                                    "id": "dest_1",
                                    "address_country": "US",
                                    "postal_code": "94043",
                                },
                            ],
                        }
                    ]
                },
            },
        )

        # 6. Complete Checkout -> checkout_session_completed
        print("\n-- 6. Complete Checkout (checkout_session_completed) --")
        resp = await client.post(
            f"/checkout-sessions/{session_id}/complete",
            json={
                "payment": {
                    "instruments": [
                        {
                            "id": "instr_card",
                            "handler_id": "com.mock.payment",
                            "type": "card",
                            "brand": "Visa",
                            "credential": {"type": "token", "token": "tok_success"},
                        },
                    ],
                },
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        completed = resp.json()
        order_id = completed["order"]["id"]
        print(f"   Status: {completed['status']}, Order: {order_id}")

        # 7. Cancel a second checkout -> checkout_session_canceled
        print("\n-- 7. Cancel Checkout (checkout_session_canceled) --")
        resp = await client.post(
            "/checkout-sessions",
            json={
                "line_items": [{"item": {"id": "bouquet_roses"}, "quantity": 1}],
                "currency": "USD",
            },
        )
        session2_id = resp.json()["id"]
        resp = await client.post(f"/checkout-sessions/{session2_id}/cancel")
        print(f"   Canceled session: {session2_id}")

        # 8. Create Cart -> cart_created
        print("\n-- 8. Create Cart (cart_created) --")
        resp = await client.post(
            "/carts",
            json={
                "line_items": [{"item": {"id": "sunflower_bunch"}, "quantity": 3}],
                "currency": "USD",
            },
        )
        cart = resp.json()
        cart_id = cart["id"]
        print(f"   Cart: {cart_id}")

        # 9. Update Cart -> cart_updated
        print("\n-- 9. Update Cart (cart_updated) --")
        resp = await client.put(
            f"/carts/{cart_id}",
            json={
                "line_items": [
                    {"item": {"id": "sunflower_bunch"}, "quantity": 2},
                    {"item": {"id": "bouquet_roses"}, "quantity": 1},
                ],
            },
        )
        print(f"   Updated cart, items: {len(resp.json()['line_items'])}")

        # 10. Get Cart -> cart_get
        print("\n-- 10. Get Cart (cart_get) --")
        resp = await client.get(f"/carts/{cart_id}")
        print(f"   Cart status: {resp.json()['status']}")

        # 11. Cancel Cart -> cart_canceled
        print("\n-- 11. Cancel Cart (cart_canceled) --")
        resp = await client.post(f"/carts/{cart_id}/cancel")
        print(f"   Cart status: {resp.json()['status']}")

        # 12. Create Order -> order_created
        print("\n-- 12. Create Order (order_created) --")
        resp = await client.post(
            "/orders",
            json={
                "checkout_id": session_id,
                "line_items": completed.get("line_items", []),
                "totals": completed.get("totals", []),
            },
        )
        direct_order = resp.json()
        direct_order_id = direct_order["id"]
        print(f"   Order: {direct_order_id}")

        # 13. Get Order (no lifecycle in body) -> order_get
        print("\n-- 13. Get Order (order_get) --")
        resp = await client.get(f"/orders/{direct_order_id}")
        print(f"   Order status: {resp.json()['status']}")

        # 13a. Update Order (PUT, REST mutation) -> order_updated
        print("\n-- 13a. Update Order (order_updated) --")
        resp = await client.put(
            f"/orders/{direct_order_id}",
            json={"label": "ORD-2026-00042"},
        )
        print(f"   Order label: {resp.json().get('label')}")

        # 14. Simulate Shipping -> order_shipped
        print("\n-- 14. Simulate Shipping (order_shipped) --")
        resp = await client.post(f"/testing/simulate-shipping/{direct_order_id}")
        print(f"   Order status: {resp.json()['status']}")

        # 15. Get Order (delivered) -> order_delivered
        print("\n-- 15. Deliver Order (order_delivered) --")
        resp = await client.post(f"/orders/{direct_order_id}/deliver")
        order_body = resp.json()
        print(f"   Order status: {order_body['status']}")
        # Record the GET to trigger order_delivered classification
        await client_tracker.record_http(
            method="GET",
            path=f"/orders/{direct_order_id}",
            status_code=200,
            response_body=order_body,
            latency_ms=5.0,
        )

        # 16. Get Order (returned) -> order_returned
        print("\n-- 16. Return Order (order_returned) --")
        resp = await client.post(f"/orders/{direct_order_id}/return")
        order_body = resp.json()
        print(f"   Order status: {order_body['status']}")
        await client_tracker.record_http(
            method="GET",
            path=f"/orders/{direct_order_id}",
            status_code=200,
            response_body=order_body,
            latency_ms=5.0,
        )

        # 17. Cancel Order -> order_canceled
        print("\n-- 17. Cancel Order (order_canceled) --")
        # Create another order to cancel
        resp = await client.post(
            "/orders",
            json={"checkout_id": session_id, "line_items": [], "totals": []},
        )
        cancel_order_id = resp.json()["id"]
        resp = await client.post(f"/orders/{cancel_order_id}/cancel")
        order_body = resp.json()
        print(f"   Order status: {order_body['status']}")
        await client_tracker.record_http(
            method="GET",
            path=f"/orders/{cancel_order_id}",
            status_code=200,
            response_body=order_body,
            latency_ms=5.0,
        )

        # 18. Identity Link Initiated -> identity_link_initiated
        print("\n-- 18. Identity Link (identity_link_initiated) --")
        resp = await client.post(
            "/identity",
            json={"provider": "google", "scope": "profile email"},
        )
        identity = resp.json()
        link_id = identity["id"]
        print(f"   Link ID: {link_id}")

        # 19. Identity Callback -> identity_link_completed
        print("\n-- 19. Identity Callback (identity_link_completed) --")
        resp = await client.get(f"/identity/callback?state={link_id}")
        print(f"   Status: {resp.json().get('status', 'linked')}")

        # 20. Identity Revoke -> identity_link_revoked
        print("\n-- 20. Identity Revoke (identity_link_revoked) --")
        resp = await client.post("/identity/revoke", json={"link_id": link_id})
        print(f"   Status: {resp.json()['status']}")

        # 21-24. Payment events (manual via record_event)
        print("\n-- 21-24. Payment Events (manual) --")
        payment_events = [
            ("payment_handler_negotiated", "Payment handler negotiated"),
            ("payment_instrument_selected", "Payment instrument selected"),
            ("payment_completed", "Payment completed"),
            ("payment_failed", "Payment failed"),
        ]
        for evt_type, label in payment_events:
            event = UCPEvent(
                event_type=evt_type,
                app_name=APP_NAME,
                checkout_session_id=session_id,
                payment_handler_id="com.mock.payment",
                payment_instrument_type="card",
                payment_brand="Visa",
                currency="USD",
                total_amount=3860,
                latency_ms=15.0,
            )
            await client_tracker.record_event(event)
            print(f"   Recorded: {label}")

        # 25. Capability Negotiated (manual via record_event)
        print("\n-- 25. Capability Negotiated --")
        event = UCPEvent(
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
        )
        await client_tracker.record_event(event)
        print("   Recorded: capability_negotiated")

        # 25a. Order webhook with no recognizable lifecycle status
        # -> order_webhook_received. We post to a webhook URL with a
        # body that intentionally has no fulfillment.events / no
        # adjustments / no legacy status, so the classifier enters
        # the webhook branch and falls all the way through to the
        # generic-receipt type.
        print("\n-- 25a. Order Webhook Received (order_webhook_received) --")
        await client_tracker.record_http(
            method="POST",
            url=str(client.base_url) + "/webhooks/orders",
            status_code=200,
            request_headers={
                "Webhook-Id": f"evt_{uuid.uuid4().hex[:10]}",
                "Webhook-Timestamp": "1767225600",
            },
            request_body={
                # Order-shaped but no lifecycle: no fulfillment.events,
                # no adjustments, no top-level status.
                "id": direct_order_id,
                "checkout_id": session_id,
            },
            response_body={"status": "ok"},
            latency_ms=4.0,
        )

        # 26. Error event -> error
        # Hits a webhook URL with status >= 400. The classifier enters
        # the webhook branch (path matches /webhook(s)) and immediately
        # returns ERROR on the 4xx status — distinct from the previous
        # "404 on /checkout-sessions/{id}" attempt, which classifier-
        # priorities through CHECKOUT_SESSION_GET first regardless of
        # status. Manual record so we don't depend on a 4xx route on
        # the test server.
        print("\n-- 26. Error event (error) --")
        await client_tracker.record_http(
            method="POST",
            url=str(client.base_url) + "/webhooks/orders",
            status_code=500,
            request_body={"reason": "test-error-trigger"},
            response_body={"error": "internal_server_error"},
            latency_ms=8.0,
        )

        # 27. Fallback request -> request (unmatched path 200)
        print("\n-- 27. Fallback Request (unmatched path) --")
        resp = await client.post("/some-unmatched-endpoint", json={})
        print(f"   Status code: {resp.status_code}")

    return session_id, order_id


# ==========================================================================
# MCP Transport — replay key operations
# ==========================================================================


# Shared response bodies for MCP/A2A replay
def _build_replay_bodies(session_id: str, order_id: str) -> dict:
    return {
        "discovery": {
            "ucp": {"version": UCP_VERSION},
            "payment": {"handlers": PAYMENT_HANDLERS},
        },
        "checkout_created": {
            "ucp": {"version": UCP_VERSION},
            "id": session_id,
            "status": "incomplete",
            "currency": "USD",
            "totals": [{"type": "total", "amount": 3261}],
            "payment": {"handlers": PAYMENT_HANDLERS},
        },
        "checkout_updated": {
            "id": session_id,
            "status": "ready_for_complete",
            "totals": [
                {"type": "subtotal", "amount": 2999},
                {"type": "fulfillment", "amount": 599},
                {"type": "total", "amount": 3860},
            ],
        },
        "checkout_completed": {
            "id": session_id,
            "status": "completed",
            "order": {
                "id": order_id,
                "permalink_url": f"https://shop.example.com/orders/{order_id}",
            },
        },
        "cart_created": {
            "id": f"cart_mcp_{uuid.uuid4().hex[:8]}",
            "status": "active",
            "currency": "USD",
            "totals": [{"type": "total", "amount": 5997}],
        },
        "order_get": {
            "id": order_id,
            "checkout_id": session_id,
            "status": "confirmed",
            "totals": [{"type": "total", "amount": 3860}],
        },
        "identity_link": {
            "provider": "google",
            "scope": "profile email",
            "status": "pending",
        },
        "identity_revoke": {"status": "revoked"},
        "capability_negotiate": {
            "ucp": {"version": UCP_VERSION},
        },
    }


async def run_mcp_transport(
    tracker: UCPAnalyticsTracker,
    session_id: str,
    order_id: str,
):
    """Replay key operations via MCP transport."""
    print("\n" + "=" * 70)
    print("  PHASE 2: MCP Transport — JSON-RPC Replay")
    print("=" * 70)

    bodies = _build_replay_bodies(session_id, order_id)

    mcp_calls = [
        ("discover_merchant", bodies["discovery"]),
        ("create_checkout", bodies["checkout_created"]),
        ("update_checkout", bodies["checkout_updated"]),
        ("complete_checkout", bodies["checkout_completed"]),
        ("create_cart", bodies["cart_created"]),
        ("get_order", bodies["order_get"]),
        ("link_identity", bodies["identity_link"]),
        ("revoke_identity", bodies["identity_revoke"]),
        ("negotiate_capability", bodies["capability_negotiate"]),
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
# A2A Transport — replay key operations
# ==========================================================================


async def run_a2a_transport(
    tracker: UCPAnalyticsTracker,
    session_id: str,
    order_id: str,
):
    """Replay key operations via A2A transport."""
    print("\n" + "=" * 70)
    print("  PHASE 3: A2A Transport — JSON-RPC Replay")
    print("=" * 70)

    bodies = _build_replay_bodies(session_id, order_id)

    a2a_calls = [
        ("a2a.ucp.discover", bodies["discovery"]),
        ("a2a.ucp.checkout.create", bodies["checkout_created"]),
        ("a2a.ucp.checkout.update", bodies["checkout_updated"]),
        ("a2a.ucp.checkout.complete", bodies["checkout_completed"]),
        ("a2a.ucp.cart.create", bodies["cart_created"]),
        ("a2a.ucp.order.get", bodies["order_get"]),
        ("a2a.ucp.identity.link", bodies["identity_link"]),
        ("a2a.ucp.identity.revoke", bodies["identity_revoke"]),
        ("a2a.ucp.capability.negotiate", bodies["capability_negotiate"]),
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
# BigQuery Verification — check every event type
# ==========================================================================


async def verify_bigquery(session_id: str):
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

    all_ok = not missing and has_rest and has_mcp and has_a2a
    if all_ok:
        print("\n   All BigQuery verifications passed!")
    else:
        print("\n   Some verifications failed — check streaming buffer delay.")

    client.close()


# ==========================================================================
# Main
# ==========================================================================


async def main():
    global server_tracker
    server_tracker = UCPAnalyticsTracker(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        table_id=TABLE_ID,
        app_name=APP_NAME,
        batch_size=1,
        auto_create_table=True,
    )
    app.add_middleware(UCPAnalyticsMiddleware, tracker=server_tracker)

    client_tracker = UCPAnalyticsTracker(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        table_id=TABLE_ID,
        app_name=APP_NAME,
        batch_size=1,
        auto_create_table=True,
    )

    # Start server
    config = uvicorn.Config(app, host="127.0.0.1", port=8199, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # Wait for server
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                await c.get("http://127.0.0.1:8199/.well-known/ucp")
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.1)

    try:
        # Phase 1: REST transport — every event type
        session_id, order_id = await run_rest_flow(client_tracker)

        # Phase 2: MCP transport replay
        await run_mcp_transport(client_tracker, session_id, order_id)

        # Phase 3: A2A transport replay
        await run_a2a_transport(client_tracker, session_id, order_id)

        # Flush all events
        print("\n   Flushing events to BigQuery...")
        await client_tracker.close()
        await server_tracker.close()

        # BigQuery verification
        await verify_bigquery(session_id)

    finally:
        server.should_exit = True
        await server_task

    print("\n   Done!")


if __name__ == "__main__":
    asyncio.run(main())
