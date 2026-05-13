#!/usr/bin/env python3
"""
End-to-End UCP Analytics Demo
==============================

A fully self-contained demo that runs:
  1. A mini UCP-compliant merchant server (FastAPI)
  2. A shopping agent client (HTTPX) with analytics hook
  3. A local analytics backend (SQLite instead of BigQuery for portability)

The demo exercises the full UCP checkout happy path:
  Discovery → Create Checkout → Update (buyer + fulfillment) → Complete → Order Shipped

Then prints the captured analytics events as a table.

Run:
    pip install fastapi uvicorn httpx
    python e2e_demo.py

No GCP credentials or BigQuery needed — uses SQLite locally.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ==========================================================================
# PART 0: Lightweight analytics (SQLite replacement for BigQuery)
# ==========================================================================
# This mirrors the real ucp_analytics package but writes to SQLite so the
# demo runs without GCP credentials.

DB_PATH = "/tmp/ucp_analytics_demo.db"


def init_db() -> sqlite3.Connection:
    """Create the analytics table matching the BigQuery schema."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ucp_events (
            event_id            TEXT PRIMARY KEY,
            event_type          TEXT NOT NULL,
            timestamp           TEXT NOT NULL,
            app_name            TEXT,
            merchant_host       TEXT,
            transport           TEXT DEFAULT 'rest',
            http_method         TEXT,
            http_path           TEXT,
            http_status_code    INTEGER,
            idempotency_key     TEXT,
            request_id          TEXT,
            checkout_session_id TEXT,
            checkout_status     TEXT,
            order_id            TEXT,
            currency            TEXT,
            subtotal_amount     INTEGER,
            tax_amount          INTEGER,
            items_discount_amount INTEGER,
            fulfillment_amount  INTEGER,
            fee_amount          INTEGER,
            discount_amount     INTEGER,
            total_amount        INTEGER,
            line_items_json     TEXT,
            line_item_count     INTEGER,
            payment_handler_id  TEXT,
            payment_instrument_type TEXT,
            payment_brand       TEXT,
            ucp_version         TEXT,
            capabilities_json   TEXT,
            extensions_json     TEXT,
            fulfillment_type    TEXT,
            fulfillment_destination_country TEXT,
            error_code          TEXT,
            error_message       TEXT,
            error_severity      TEXT,
            messages_json       TEXT,
            latency_ms          REAL,
            custom_metadata_json TEXT
        )
    """)
    conn.commit()
    return conn


# --- Inline parser (same logic as ucp_analytics.parser) ---


def classify_event(method: str, path: str, status_code: int, body: dict | None) -> str:
    m, p = method.upper(), path.rstrip("/")
    if p.endswith("/.well-known/ucp"):
        return "profile_discovered"
    if re.search(r"/checkout-sessions/?$", p) and m == "POST":
        return "checkout_session_created"
    if re.search(r"/checkout-sessions/[^/]+/complete$", p) and m == "POST":
        return "checkout_session_completed"
    if re.search(r"/checkout-sessions/[^/]+/cancel$", p) and m == "POST":
        return "checkout_session_canceled"
    if re.search(r"/checkout-sessions/[^/]+$", p) and m == "PUT":
        if body and body.get("status") == "requires_escalation":
            return "checkout_escalation"
        return "checkout_session_updated"
    if re.search(r"/checkout-sessions/[^/]+$", p) and m == "GET":
        return "checkout_session_get"
    if "/simulate-shipping" in p:
        return "order_shipped"
    if "/orders" in p:
        return "order_created" if m == "POST" else "order_updated"
    if status_code and status_code >= 400:
        return "error"
    return "request"


def extract_fields(body: dict | None) -> Dict[str, Any]:
    if not body or not isinstance(body, dict):
        return {}
    r: Dict[str, Any] = {}
    raw_id = body.get("id", "")
    if raw_id:
        if "checkout_id" in body:
            r["order_id"] = str(raw_id)
            r["checkout_session_id"] = body["checkout_id"]
        else:
            r["checkout_session_id"] = str(raw_id)
    if "order_id" in body:
        r["order_id"] = body["order_id"]
    if "status" in body:
        r["checkout_status"] = body["status"]
    if "currency" in body:
        r["currency"] = body["currency"]
    for t in body.get("totals", []):
        if isinstance(t, dict) and "amount" in t:
            mapping = {
                "items_discount": "items_discount_amount",
                "subtotal": "subtotal_amount",
                "discount": "discount_amount",
                "fulfillment": "fulfillment_amount",
                "tax": "tax_amount",
                "fee": "fee_amount",
                "total": "total_amount",
            }
            key = mapping.get(t.get("type", ""))
            if key:
                r[key] = t["amount"]
    items = body.get("line_items")
    if isinstance(items, list) and items:
        r["line_item_count"] = len(items)
        r["line_items_json"] = json.dumps(items, default=str)
    ucp = body.get("ucp")
    if isinstance(ucp, dict):
        r["ucp_version"] = ucp.get("version")
        caps = ucp.get("capabilities", [])
        if caps:
            r["capabilities_json"] = json.dumps(caps)
            exts = [c for c in caps if isinstance(c, dict) and c.get("extends")]
            if exts:
                r["extensions_json"] = json.dumps(exts)
    # Check payment_data first (from complete response), then payment (from create)
    payment_data = body.get("payment_data")
    payment = body.get("payment")
    if isinstance(payment_data, dict) and payment_data:
        r["payment_handler_id"] = payment_data.get("handler_id") or payment_data.get(
            "id"
        )
        r["payment_instrument_type"] = payment_data.get("type")
        r["payment_brand"] = payment_data.get("brand")
    elif isinstance(payment, dict) and payment:
        handlers = payment.get("handlers", [])
        if handlers:
            r["payment_handler_id"] = handlers[0].get("id")
            r["payment_instrument_type"] = handlers[0].get("type")
            r["payment_brand"] = handlers[0].get("brand")
        else:
            r["payment_handler_id"] = payment.get("handler_id") or payment.get("id")
            r["payment_instrument_type"] = payment.get("type")
            r["payment_brand"] = payment.get("brand")
    ff = body.get("fulfillment")
    if isinstance(ff, dict):
        methods = ff.get("methods") or ff.get("expectations") or []
        if isinstance(methods, list) and methods:
            r["fulfillment_type"] = methods[0].get("type") or methods[0].get(
                "method_type"
            )
            dests = methods[0].get("destinations") or methods[0].get("destination")
            if isinstance(dests, list) and dests:
                r["fulfillment_destination_country"] = dests[0].get("address_country")
            elif isinstance(dests, dict):
                r["fulfillment_destination_country"] = dests.get("address_country")
    msgs = body.get("messages")
    if isinstance(msgs, list) and msgs:
        r["messages_json"] = json.dumps(msgs)
        for msg in msgs:
            if isinstance(msg, dict) and msg.get("type") == "error":
                r["error_code"] = msg.get("code")
                r["error_message"] = msg.get("content")
                r["error_severity"] = msg.get("severity")
                break
    links = body.get("links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and link.get("type") == "order":
                r["order_id"] = r.get("order_id") or link.get("url")
    return {k: v for k, v in r.items() if v is not None}


# --- Local analytics writer ---


class LocalAnalyticsTracker:
    """Writes to SQLite instead of BigQuery for the demo."""

    def __init__(self, conn: sqlite3.Connection, app_name: str = ""):
        self.conn = conn
        self.app_name = app_name
        self.events: List[Dict[str, Any]] = []

    def record_http(
        self,
        method: str,
        path: str,
        status_code: int,
        response_body: dict | None,
        latency_ms: float | None = None,
        headers: dict | None = None,
    ):
        headers = headers or {}
        event_type = classify_event(method, path, status_code, response_body)
        row: Dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "app_name": self.app_name,
            "merchant_host": "localhost:8199",
            "transport": "rest",
            "http_method": method.upper(),
            "http_path": path,
            "http_status_code": status_code,
            "idempotency_key": headers.get("idempotency-key", ""),
            "request_id": headers.get("request-id", ""),
            "latency_ms": latency_ms,
        }
        fields = extract_fields(response_body)
        row.update(fields)
        self.events.append(row)

        # Write to SQLite
        cols = list(row.keys())
        vals = [row[c] for c in cols]
        placeholders = ",".join(["?"] * len(cols))
        self.conn.execute(
            f"INSERT INTO ucp_events ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self.conn.commit()
        return row


# ==========================================================================
# PART 1: Mini UCP Merchant Server (FastAPI)
# ==========================================================================
# A minimal UCP-compliant server that implements:
#   - GET  /.well-known/ucp          → Discovery profile
#   - POST /checkout-sessions        → Create checkout
#   - PUT  /checkout-sessions/{id}   → Update checkout
#   - POST /checkout-sessions/{id}/complete → Complete checkout
#   - POST /testing/simulate-shipping/{id}  → Simulate order shipped

# In-memory store for the demo
SESSIONS: Dict[str, dict] = {}
ORDERS: Dict[str, dict] = {}

UCP_VERSION = "2026-01-11"
UCP_ENVELOPE = {
    "version": UCP_VERSION,
    "capabilities": [
        {
            "name": "dev.ucp.shopping.checkout",
            "version": UCP_VERSION,
            "spec": "https://ucp.dev/specs/shopping/checkout",
        },
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
}

# Product catalog
PRODUCTS = {
    "bouquet_roses": {"title": "Red Rose Bouquet", "price": 2999},
    "tulip_arrangement": {"title": "Tulip Arrangement", "price": 4499},
    "sunflower_bunch": {"title": "Sunflower Bunch", "price": 1999},
}

server_tracker: LocalAnalyticsTracker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="UCP Flower Shop (Demo)", lifespan=lifespan)


@app.get("/.well-known/ucp")
async def discovery():
    """UCP Business Discovery Profile."""
    return {
        "ucp": {
            "version": UCP_VERSION,
            "services": {
                "dev.ucp.shopping": {
                    "version": UCP_VERSION,
                    "rest": {
                        "endpoint": "http://localhost:8199/",
                        "schema": "http://localhost:8199/openapi.json",
                    },
                },
            },
            "capabilities": UCP_ENVELOPE["capabilities"],
        },
        "payment": {
            "handlers": [
                {
                    "id": "mock_payment_handler",
                    "name": "Mock Payment Handler",
                    "version": UCP_VERSION,
                },
            ],
        },
    }


@app.post("/checkout-sessions")
async def create_checkout(request: Request):
    """Create a new UCP checkout session."""
    body = await request.json()
    session_id = f"chk_{uuid.uuid4().hex[:12]}"

    # Resolve line items with prices
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

    tax = round(subtotal * 0.0875)  # 8.75% CA tax
    total = subtotal + tax

    session = {
        "ucp": UCP_ENVELOPE,
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
            "handlers": [
                {
                    "id": "mock_payment_handler",
                    "name": "Mock Payment Handler",
                    "version": UCP_VERSION,
                },
            ],
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
    }

    SESSIONS[session_id] = session
    return JSONResponse(session, status_code=201)


@app.put("/checkout-sessions/{session_id}")
async def update_checkout(session_id: str, request: Request):
    """Update an existing checkout session (add buyer, fulfillment, etc.)."""
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)

    body = await request.json()
    session = SESSIONS[session_id]

    # Merge buyer
    if "buyer" in body:
        session["buyer"] = body["buyer"]

    # Merge fulfillment
    if "fulfillment" in body:
        session["fulfillment"] = body["fulfillment"]
        # Add shipping cost
        shipping = 599
        session["totals"].append({"type": "fulfillment", "amount": shipping})
        # Recalculate total
        subtotal = next(
            t["amount"] for t in session["totals"] if t["type"] == "subtotal"
        )
        tax = next(t["amount"] for t in session["totals"] if t["type"] == "tax")
        session["totals"] = [t for t in session["totals"] if t["type"] != "total"]
        session["totals"].append({"type": "total", "amount": subtotal + tax + shipping})

    # Apply discount code
    if "discount" in body:
        discount_amount = 500  # flat $5 discount
        session["totals"].append({"type": "discount", "amount": discount_amount})
        total_entry = next(t for t in session["totals"] if t["type"] == "total")
        total_entry["amount"] -= discount_amount

    # Check readiness
    has_buyer = bool(session.get("buyer", {}).get("email"))
    has_fulfillment = "fulfillment" in session
    if has_buyer and has_fulfillment:
        session["status"] = "ready_for_complete"
    else:
        session["status"] = "incomplete"

    SESSIONS[session_id] = session
    return JSONResponse(session)


@app.post("/checkout-sessions/{session_id}/complete")
async def complete_checkout(session_id: str, request: Request):
    """Complete the checkout session → create order."""
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)

    body = await request.json()
    session = SESSIONS[session_id]

    # Validate payment
    payment_data = body.get("payment_data", {})
    if not payment_data:
        session["status"] = "requires_escalation"
        session["messages"] = [
            {
                "type": "error",
                "code": "missing_payment",
                "content": "Payment data required",
                "severity": "escalation",
            },
        ]
        SESSIONS[session_id] = session
        return JSONResponse(session, status_code=400)

    # Create order
    order_id = f"order_{uuid.uuid4().hex[:10]}"
    session["status"] = "completed"
    session["order_id"] = order_id
    session["payment_data"] = payment_data

    ORDERS[order_id] = {
        "id": order_id,
        "checkout_id": session_id,
        "status": "confirmed",
        "line_items": session["line_items"],
        "totals": session["totals"],
    }

    SESSIONS[session_id] = session
    return JSONResponse(session)


@app.post("/testing/simulate-shipping/{order_id}")
async def simulate_shipping(order_id: str):
    """Simulate order shipment (testing endpoint)."""
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)

    ORDERS[order_id]["status"] = "shipped"
    ORDERS[order_id]["tracking"] = {
        "carrier": "USPS",
        "tracking_number": "9400111899223456789012",
    }

    return JSONResponse(ORDERS[order_id])


# ==========================================================================
# PART 2: Analytics-Instrumented HTTPX Client (Agent Side)
# ==========================================================================


async def run_shopping_agent(tracker: LocalAnalyticsTracker):
    """Simulate a full shopping agent journey with analytics."""

    print("\n" + "=" * 70)
    print("  🛍️  UCP SHOPPING AGENT — ANALYTICS E2E DEMO")
    print("=" * 70)

    async with httpx.AsyncClient(
        base_url="http://localhost:8199",
        headers={
            "UCP-Agent": 'profile="https://agent.example.com/profile"',
            "Content-Type": "application/json",
        },
    ) as client:
        # ------------------------------------------------------------------
        # Step 1: Discovery
        # ------------------------------------------------------------------
        print("\n── Step 1: Discover Merchant Capabilities ──────────────────")
        start = time.monotonic()
        resp = await client.get("/.well-known/ucp")
        latency = round((time.monotonic() - start) * 1000, 2)
        profile = resp.json()

        tracker.record_http(
            "GET", "/.well-known/ucp", resp.status_code, profile, latency
        )

        caps = [c["name"] for c in profile["ucp"]["capabilities"]]
        print(f"   UCP version: {profile['ucp']['version']}")
        print(f"   Capabilities: {caps}")
        print(
            f"   Payment handlers: {[h['id'] for h in profile['payment']['handlers']]}"
        )
        print(f"   ⏱  {latency}ms")

        # ------------------------------------------------------------------
        # Step 2: Create Checkout
        # ------------------------------------------------------------------
        print("\n── Step 2: Create Checkout Session ─────────────────────────")
        start = time.monotonic()
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
            headers={
                "idempotency-key": str(uuid.uuid4()),
                "request-id": str(uuid.uuid4()),
            },
        )
        latency = round((time.monotonic() - start) * 1000, 2)
        checkout = resp.json()

        tracker.record_http(
            "POST",
            "/checkout-sessions",
            resp.status_code,
            checkout,
            latency,
            dict(resp.request.headers),
        )

        session_id = checkout["id"]
        total = next(t["amount"] for t in checkout["totals"] if t["type"] == "total")
        print(f"   Session ID: {session_id}")
        print(f"   Status: {checkout['status']}")
        print(f"   Items: {len(checkout['line_items'])}")
        print(f"   Total: ${total / 100:.2f} {checkout['currency']}")
        print(f"   ⏱  {latency}ms")

        # ------------------------------------------------------------------
        # Step 3: Update — Add buyer email + fulfillment
        # ------------------------------------------------------------------
        print("\n── Step 3: Update — Add Buyer Email & Shipping ─────────────")
        start = time.monotonic()
        resp = await client.put(
            f"/checkout-sessions/{session_id}",
            json={
                "buyer": {
                    "full_name": "Jane Doe",
                    "email": "jane@example.com",
                    "phone": "555-123-4567",
                },
                "fulfillment": {
                    "expectations": [
                        {
                            "id": "ship_standard",
                            "method_type": "shipping",
                            "destination": {
                                "id": "home",
                                "address_country": "US",
                                "address_region": "CA",
                                "postal_code": "94043",
                                "street_address": "123 Main St",
                            },
                            "line_items": [
                                {"item_id": "bouquet_roses", "quantity": 2},
                                {"item_id": "sunflower_bunch", "quantity": 1},
                            ],
                        }
                    ]
                },
            },
        )
        latency = round((time.monotonic() - start) * 1000, 2)
        updated = resp.json()

        tracker.record_http(
            "PUT",
            f"/checkout-sessions/{session_id}",
            resp.status_code,
            updated,
            latency,
        )

        total = next(t["amount"] for t in updated["totals"] if t["type"] == "total")
        print(f"   Status: {updated['status']}")
        print(f"   Total (with shipping): ${total / 100:.2f}")
        ff_method = updated["fulfillment"]["expectations"][0]["method_type"]
        dest = updated["fulfillment"]["expectations"][0]["destination"]
        ff_country = dest["address_country"]
        print(f"   Fulfillment: {ff_method}")
        print(f"   Destination: {ff_country}")
        print(f"   ⏱  {latency}ms")

        # ------------------------------------------------------------------
        # Step 4: Update — Apply discount code
        # ------------------------------------------------------------------
        print("\n── Step 4: Apply Discount Code ─────────────────────────────")
        start = time.monotonic()
        resp = await client.put(
            f"/checkout-sessions/{session_id}",
            json={"discount": {"code": "FLOWERS10"}},
        )
        latency = round((time.monotonic() - start) * 1000, 2)
        discounted = resp.json()

        tracker.record_http(
            "PUT",
            f"/checkout-sessions/{session_id}",
            resp.status_code,
            discounted,
            latency,
        )

        total = next(t["amount"] for t in discounted["totals"] if t["type"] == "total")
        discount = next(
            (t["amount"] for t in discounted["totals"] if t["type"] == "discount"), 0
        )
        print(f"   Discount applied: -${discount / 100:.2f}")
        print(f"   New total: ${total / 100:.2f}")
        print(f"   ⏱  {latency}ms")

        # ------------------------------------------------------------------
        # Step 5: Complete Checkout (with payment)
        # ------------------------------------------------------------------
        print("\n── Step 5: Complete Checkout ────────────────────────────────")
        start = time.monotonic()
        resp = await client.post(
            f"/checkout-sessions/{session_id}/complete",
            json={
                "payment_data": {
                    "id": "instr_my_card",
                    "handler_id": "mock_payment_handler",
                    "type": "card",
                    "brand": "Visa",
                    "last_digits": "4242",
                    "credential": {"type": "token", "token": "success_token"},
                },
            },
            headers={
                "idempotency-key": str(uuid.uuid4()),
                "request-id": str(uuid.uuid4()),
            },
        )
        latency = round((time.monotonic() - start) * 1000, 2)
        completed = resp.json()

        tracker.record_http(
            "POST",
            f"/checkout-sessions/{session_id}/complete",
            resp.status_code,
            completed,
            latency,
            dict(resp.request.headers),
        )

        print(f"   Status: {completed['status']}")
        print(f"   Order ID: {completed.get('order_id')}")
        pd = completed["payment_data"]
        print(f"   Payment: {pd['brand']} ****{pd['last_digits']}")
        print(f"   ⏱  {latency}ms")

        order_id = completed["order_id"]

        # ------------------------------------------------------------------
        # Step 6: Simulate shipping
        # ------------------------------------------------------------------
        print("\n── Step 6: Simulate Order Shipped ──────────────────────────")
        start = time.monotonic()
        resp = await client.post(f"/testing/simulate-shipping/{order_id}")
        latency = round((time.monotonic() - start) * 1000, 2)
        shipped = resp.json()

        tracker.record_http(
            "POST",
            f"/testing/simulate-shipping/{order_id}",
            resp.status_code,
            shipped,
            latency,
        )

        print(f"   Order status: {shipped['status']}")
        print(f"   Carrier: {shipped['tracking']['carrier']}")
        print(f"   Tracking: {shipped['tracking']['tracking_number']}")
        print(f"   ⏱  {latency}ms")

    return session_id, order_id


# ==========================================================================
# PART 3: Analytics Report (query the local SQLite)
# ==========================================================================


def print_analytics_report(conn: sqlite3.Connection, session_id: str):
    """Print captured analytics like you'd see in BigQuery."""

    print("\n" + "=" * 70)
    print("  📊  ANALYTICS REPORT (from captured UCP events)")
    print("=" * 70)

    # --- Session timeline ---
    print("\n── Session Timeline ────────────────────────────────────────")
    print(f"{'#':<3} {'Event Type':<30} {'Status':<22} {'Total':>9} {'Latency':>8}")
    print("─" * 75)

    cursor = conn.execute("""
        SELECT
            event_type, checkout_status, total_amount,
            latency_ms, http_method, http_path, http_status_code
        FROM ucp_events
        ORDER BY timestamp
    """)
    for i, row in enumerate(cursor.fetchall(), 1):
        event_type, status, total, latency, method, path, code = row
        total_str = f"${total / 100:.2f}" if total else ""
        status_str = status or ""
        latency_str = f"{latency:.0f}ms" if latency else ""
        print(
            f"{i:<3} {event_type:<30} {status_str:<22} {total_str:>9} {latency_str:>8}"
        )

    # --- Checkout funnel ---
    print("\n── Checkout Funnel ─────────────────────────────────────────")
    cursor = conn.execute("""
        SELECT event_type, COUNT(*) as cnt
        FROM ucp_events
        WHERE event_type LIKE 'checkout_%' OR event_type = 'profile_discovered'
        GROUP BY event_type
        ORDER BY cnt DESC
    """)
    for event_type, cnt in cursor.fetchall():
        bar = "█" * (cnt * 10)
        print(f"   {event_type:<35} {cnt:>3}  {bar}")

    # --- Financial summary ---
    print("\n── Financial Summary ───────────────────────────────────────")
    cursor = conn.execute("""
        SELECT
            COALESCE(currency, 'USD') as currency,
            MAX(subtotal_amount) as subtotal,
            MAX(tax_amount) as tax,
            MAX(fulfillment_amount) as fulfillment,
            MAX(discount_amount) as discount,
            MAX(total_amount) as total
        FROM ucp_events
        WHERE checkout_session_id IS NOT NULL
    """)
    for row in cursor.fetchall():
        currency, subtotal, tax, fulfillment, discount, total = row
        print(f"   Currency:  {currency}")
        print(f"   Subtotal:  ${(subtotal or 0) / 100:.2f}")
        print(f"   Tax:       ${(tax or 0) / 100:.2f}")
        print(f"   Fulfillment: ${(fulfillment or 0) / 100:.2f}")
        print(f"   Discount: -${(discount or 0) / 100:.2f}")
        print("   ─────────────────")
        print(f"   Total:     ${(total or 0) / 100:.2f}")

    # --- Payment details ---
    print("\n── Payment ─────────────────────────────────────────────────")
    cursor = conn.execute("""
        SELECT payment_handler_id, payment_brand, payment_instrument_type
        FROM ucp_events
        WHERE event_type = 'checkout_session_completed'
    """)
    for handler, brand, inst_type in cursor.fetchall():
        print(f"   Handler: {handler}")
        print(f"   Brand:   {brand}")
        print(f"   Type:    {inst_type}")

    # --- Capabilities discovered ---
    print("\n── Capabilities Discovered ─────────────────────────────────")
    cursor = conn.execute("""
        SELECT capabilities_json FROM ucp_events
        WHERE event_type = 'profile_discovered' AND capabilities_json IS NOT NULL
        LIMIT 1
    """)
    row = cursor.fetchone()
    if row and row[0]:
        caps = json.loads(row[0])
        for cap in caps:
            extends = f" (extends {cap['extends']})" if cap.get("extends") else ""
            print(f"   ✓ {cap['name']}{extends}")

    # --- Latency stats ---
    print("\n── Latency Percentiles ─────────────────────────────────────")
    cursor = conn.execute("""
        SELECT
            event_type,
            COUNT(*) as calls,
            MIN(latency_ms) as min_ms,
            AVG(latency_ms) as avg_ms,
            MAX(latency_ms) as max_ms
        FROM ucp_events
        WHERE latency_ms IS NOT NULL
        GROUP BY event_type
        ORDER BY avg_ms DESC
    """)
    print(f"   {'Event Type':<30} {'Calls':>5} {'Min':>8} {'Avg':>8} {'Max':>8}")
    print("   " + "─" * 63)
    for event_type, calls, min_ms, avg_ms, max_ms in cursor.fetchall():
        stats = f"{min_ms:>7.1f} {avg_ms:>7.1f} {max_ms:>7.1f}"
        print(f"   {event_type:<30} {calls:>5} {stats}")

    # --- Total events ---
    cursor = conn.execute("SELECT COUNT(*) FROM ucp_events")
    total_events = cursor.fetchone()[0]
    print(f"\n   Total events captured: {total_events}")

    # --- Equivalent BigQuery queries ---
    print("\n── Equivalent BigQuery Queries ─────────────────────────────")
    print(
        """
    -- Checkout funnel
    SELECT event_type, COUNT(*) as cnt
    FROM `my-project.ucp_analytics.ucp_events`
    WHERE checkout_session_id = '{session_id}'
    GROUP BY event_type ORDER BY MIN(timestamp);

    -- Revenue by merchant
    SELECT merchant_host, SUM(total_amount)/100.0 as revenue
    FROM `my-project.ucp_analytics.ucp_events`
    WHERE event_type = 'checkout_session_completed'
    GROUP BY merchant_host;
    """.replace("{session_id}", session_id)
    )


# ==========================================================================
# PART 4: Main — wire everything together
# ==========================================================================


async def main():
    import os

    # Clean up previous run
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = init_db()
    tracker = LocalAnalyticsTracker(conn, app_name="flower_shop_demo")

    # Start the server in the background
    config = uvicorn.Config(app, host="127.0.0.1", port=8199, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # Wait for server to be ready
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                await c.get("http://127.0.0.1:8199/.well-known/ucp")
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.1)

    try:
        # Run the shopping agent
        session_id, order_id = await run_shopping_agent(tracker)

        # Print analytics
        print_analytics_report(conn, session_id)

    finally:
        server.should_exit = True
        await server_task
        conn.close()

    print("\n✅ Demo complete. Analytics DB saved to:", DB_PATH)
    print("   Inspect with: sqlite3", DB_PATH, '"SELECT * FROM ucp_events;"')


if __name__ == "__main__":
    asyncio.run(main())
