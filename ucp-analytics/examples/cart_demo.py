#!/usr/bin/env python3
"""
Cart Demo — Full Cart Lifecycle + Checkout Conversion (BigQuery)
=================================================================

Exercises:
  1. Cart CRUD — create, get, update (add/remove items), get
  2. Cart cancellation
  3. Cart-to-checkout conversion — create cart, then POST /checkout-sessions
     with cart_id, complete checkout

Uses UCP SDK Pydantic models for typed request construction
and UCPAnalyticsTracker -> BigQuery for analytics.

Event types covered:
  CART_CREATED, CART_GET, CART_UPDATED, CART_CANCELED,
  CHECKOUT_SESSION_CREATED, CHECKOUT_SESSION_COMPLETED

Run:
    uv run python examples/cart_demo.py

Requires:
    - gcloud auth application-default login
    - BigQuery API enabled
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Dict

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# UCP SDK models for typed request construction
from ucp_sdk.models.schemas.shopping.types.item_create_req import ItemCreateRequest
from ucp_sdk.models.schemas.shopping.types.line_item_create_req import (
    LineItemCreateRequest,
)

from ucp_analytics import UCPAnalyticsTracker

sys.path.insert(0, os.path.dirname(__file__))
from _demo_utils import create_tracker, verify_bigquery

# ======================================================================
# Mini UCP Server with cart endpoints
# ======================================================================

CARTS: Dict[str, dict] = {}
SESSIONS: Dict[str, dict] = {}
UCP_VERSION = "2026-01-11"

PRODUCTS = {
    "roses": {"title": "Red Roses", "price": 2999},
    "tulips": {"title": "Tulip Bouquet", "price": 1999},
    "sunflowers": {"title": "Sunflower Bunch", "price": 2499},
    "lilies": {"title": "Lily Arrangement", "price": 3499},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="UCP Cart Demo Server", lifespan=lifespan)


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


@app.get("/carts/{cart_id}")
async def get_cart(cart_id: str):
    if cart_id not in CARTS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(CARTS[cart_id])


@app.put("/carts/{cart_id}")
async def update_cart(cart_id: str, request: Request):
    if cart_id not in CARTS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    body = await request.json()
    cart = CARTS[cart_id]

    # Add items
    for item in body.get("add_items", []):
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
        cart["line_items"].append(li)

    # Remove items
    remove_ids = body.get("remove_item_ids", [])
    if remove_ids:
        cart["line_items"] = [
            li for li in cart["line_items"] if li["id"] not in remove_ids
        ]

    # Recalculate totals
    subtotal = sum(li["item"]["price"] * li["quantity"] for li in cart["line_items"])
    cart["totals"] = [
        {"type": "subtotal", "amount": subtotal},
        {"type": "total", "amount": subtotal},
    ]
    CARTS[cart_id] = cart
    return JSONResponse(cart)


@app.post("/carts/{cart_id}/cancel")
async def cancel_cart(cart_id: str):
    if cart_id not in CARTS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    CARTS[cart_id]["status"] = "canceled"
    return JSONResponse(CARTS[cart_id])


@app.post("/checkout-sessions")
async def create_checkout(request: Request):
    body = await request.json()
    session_id = f"chk_{uuid.uuid4().hex[:12]}"

    # Cart-to-checkout conversion
    cart_id = body.get("cart_id")
    if cart_id and cart_id in CARTS:
        cart = CARTS[cart_id]
        session = {
            "id": session_id,
            "status": "incomplete",
            "currency": cart["currency"],
            "line_items": cart["line_items"],
            "totals": cart["totals"],
            "cart_id": cart_id,
            "payment": {
                "handlers": [
                    {"id": "mock_handler", "name": "Mock Pay", "version": UCP_VERSION}
                ],
            },
        }
    else:
        session = {
            "id": session_id,
            "status": "incomplete",
            "currency": "USD",
            "line_items": body.get("line_items", []),
            "totals": [{"type": "total", "amount": 0}],
            "payment": {
                "handlers": [
                    {"id": "mock_handler", "name": "Mock Pay", "version": UCP_VERSION}
                ],
            },
        }

    SESSIONS[session_id] = session
    return JSONResponse(session, status_code=201)


@app.post("/checkout-sessions/{session_id}/complete")
async def complete_checkout(session_id: str, request: Request):
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    body = await request.json()
    session = SESSIONS[session_id]
    session["status"] = "completed"
    session["order_id"] = f"order_{uuid.uuid4().hex[:10]}"
    session["payment_data"] = body.get("payment_data", {})
    SESSIONS[session_id] = session
    return JSONResponse(session)


# ======================================================================
# Cart scenario runner
# ======================================================================


def _make_line_items(*items: tuple[str, int]) -> list[dict]:
    """Build a line_items list using SDK models."""
    return [
        LineItemCreateRequest(
            item=ItemCreateRequest(id=item_id),
            quantity=qty,
        ).model_dump(exclude_none=True)
        for item_id, qty in items
    ]


async def run_cart_demo(tracker: UCPAnalyticsTracker):
    print("\n" + "=" * 70)
    print("  UCP CART DEMO — Full Cart Lifecycle")
    print("=" * 70)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8197") as client:
        # --- Flow 1: Cart CRUD ---
        print("\n-- Flow 1: Cart CRUD --")

        # Create cart
        start = time.monotonic()
        resp = await client.post(
            "/carts",
            json={
                "line_items": _make_line_items(("roses", 2), ("tulips", 1)),
                "currency": "USD",
            },
        )
        latency = round((time.monotonic() - start) * 1000, 2)
        cart = resp.json()
        cart_id = cart["id"]
        await tracker.record_http(
            method="POST",
            path="/carts",
            status_code=resp.status_code,
            response_body=cart,
            latency_ms=latency,
        )
        print(f"   Created cart: {cart_id} ({len(cart['line_items'])} items)")

        # Get cart
        resp = await client.get(f"/carts/{cart_id}")
        await tracker.record_http(
            method="GET",
            path=f"/carts/{cart_id}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Got cart: status={resp.json()['status']}")

        # Update: add item
        first_li_id = cart["line_items"][0]["id"]
        resp = await client.put(
            f"/carts/{cart_id}",
            json={
                "add_items": _make_line_items(("sunflowers", 1)),
            },
        )
        updated = resp.json()
        await tracker.record_http(
            method="PUT",
            path=f"/carts/{cart_id}",
            status_code=resp.status_code,
            response_body=updated,
        )
        print(f"   Added sunflowers: {len(updated['line_items'])} items")

        # Update: remove item
        resp = await client.put(
            f"/carts/{cart_id}",
            json={"remove_item_ids": [first_li_id]},
        )
        updated = resp.json()
        await tracker.record_http(
            method="PUT",
            path=f"/carts/{cart_id}",
            status_code=resp.status_code,
            response_body=updated,
        )
        print(f"   Removed first item: {len(updated['line_items'])} items")

        # Get cart (final state)
        resp = await client.get(f"/carts/{cart_id}")
        await tracker.record_http(
            method="GET",
            path=f"/carts/{cart_id}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        total = next(t["amount"] for t in resp.json()["totals"] if t["type"] == "total")
        print(f"   Final cart total: ${total / 100:.2f}")

        # --- Flow 2: Cart cancellation ---
        print("\n-- Flow 2: Cart Cancellation --")
        resp = await client.post(
            "/carts",
            json={"line_items": _make_line_items(("lilies", 1))},
        )
        cart2 = resp.json()
        cart2_id = cart2["id"]
        await tracker.record_http(
            method="POST",
            path="/carts",
            status_code=resp.status_code,
            response_body=cart2,
        )
        print(f"   Created cart: {cart2_id}")

        resp = await client.post(f"/carts/{cart2_id}/cancel")
        await tracker.record_http(
            method="POST",
            path=f"/carts/{cart2_id}/cancel",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Canceled: status={resp.json()['status']}")

        # --- Flow 3: Cart-to-checkout conversion ---
        print("\n-- Flow 3: Cart to Checkout Conversion --")
        resp = await client.post(
            "/carts",
            json={
                "line_items": _make_line_items(("roses", 3), ("lilies", 1)),
                "currency": "USD",
            },
        )
        cart3 = resp.json()
        cart3_id = cart3["id"]
        await tracker.record_http(
            method="POST",
            path="/carts",
            status_code=resp.status_code,
            response_body=cart3,
        )
        print(f"   Created cart: {cart3_id}")

        # Convert to checkout
        resp = await client.post(
            "/checkout-sessions",
            json={"cart_id": cart3_id},
        )
        checkout = resp.json()
        session_id = checkout["id"]
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=resp.status_code,
            response_body=checkout,
        )
        print(f"   Converted to checkout: {session_id}")
        print(f"   Items carried over: {len(checkout['line_items'])}")

        # Complete checkout
        resp = await client.post(
            f"/checkout-sessions/{session_id}/complete",
            json={
                "payment_data": {
                    "handler_id": "mock_handler",
                    "type": "card",
                    "brand": "Visa",
                    "credential": {"token": "success"},
                }
            },
        )
        completed = resp.json()
        await tracker.record_http(
            method="POST",
            path=f"/checkout-sessions/{session_id}/complete",
            status_code=resp.status_code,
            response_body=completed,
        )
        print(f"   Completed: order_id={completed.get('order_id')}")


# ======================================================================
# Main
# ======================================================================


async def main():
    tracker = create_tracker("cart_demo")

    config = uvicorn.Config(app, host="127.0.0.1", port=8197, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                await c.get("http://127.0.0.1:8197/carts/ping")
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.1)

    try:
        await run_cart_demo(tracker)

        print("\n   Flushing events to BigQuery...")
        await tracker.close()

        await verify_bigquery("cart_demo", "Cart Analytics Report")
    finally:
        server.should_exit = True
        await server_task

    print("\nDemo complete.")


if __name__ == "__main__":
    asyncio.run(main())
