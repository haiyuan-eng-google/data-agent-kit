#!/usr/bin/env python3
"""
Order Lifecycle Demo (BigQuery)
================================

Simulates webhook-delivered order events through the full lifecycle:
  1. Happy path to delivery — order created -> shipped -> delivered
  2. Order cancellation — order created -> canceled
  3. Return after delivery — shipped -> delivered -> returned
  4. Fulfillment variants — shipping, pickup, digital

Uses UCP SDK Pydantic models for type context
and UCPAnalyticsTracker -> BigQuery for analytics.

Event types covered:
  ORDER_CREATED, ORDER_UPDATED, ORDER_SHIPPED, ORDER_DELIVERED,
  ORDER_RETURNED, ORDER_CANCELED, CHECKOUT_SESSION_CREATED,
  CHECKOUT_SESSION_COMPLETED

Run:
    uv run python examples/order_lifecycle_demo.py

Requires:
    - gcloud auth application-default login
    - BigQuery API enabled
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Dict

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ucp_analytics import UCPAnalyticsTracker

sys.path.insert(0, os.path.dirname(__file__))
from _demo_utils import create_tracker, verify_bigquery

# ======================================================================
# Mini UCP Server with order lifecycle
# ======================================================================

SESSIONS: Dict[str, dict] = {}
ORDERS: Dict[str, dict] = {}
UCP_VERSION = "2026-01-11"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="UCP Order Lifecycle Demo", lifespan=lifespan)


@app.post("/checkout-sessions")
async def create_checkout(request: Request):
    body = await request.json()
    session_id = f"chk_{uuid.uuid4().hex[:12]}"
    fulfillment_type = body.get("fulfillment_type", "shipping")
    session = {
        "id": session_id,
        "status": "incomplete",
        "currency": "USD",
        "line_items": body.get("line_items", []),
        "totals": [
            {"type": "subtotal", "amount": 4999},
            {"type": "total", "amount": 4999},
        ],
        "fulfillment": {
            "methods": [
                {
                    "id": "m1",
                    "type": fulfillment_type,
                    "destinations": [{"address_country": "US"}],
                }
            ]
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
    order_id = f"order_{uuid.uuid4().hex[:10]}"
    session["status"] = "completed"
    session["order_id"] = order_id
    session["payment_data"] = body.get("payment_data", {})

    # Create order
    fulfillment = session.get("fulfillment", {})
    methods = fulfillment.get("methods", [{}])
    ff_type = methods[0].get("type", "shipping") if methods else "shipping"
    ORDERS[order_id] = {
        "id": order_id,
        "checkout_id": session_id,
        "status": "confirmed",
        "currency": "USD",
        "line_items": session["line_items"],
        "totals": session["totals"],
        "fulfillment": {
            "expectations": [
                {
                    "method_type": ff_type,
                    "status": "pending",
                    "destination": {"address_country": "US"},
                }
            ]
        },
    }
    SESSIONS[session_id] = session
    return JSONResponse(session)


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(ORDERS[order_id])


@app.post("/orders/{order_id}/ship")
async def ship_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    ORDERS[order_id]["status"] = "shipped"
    ORDERS[order_id]["fulfillment"]["expectations"][0]["status"] = "shipped"
    return JSONResponse(ORDERS[order_id])


@app.post("/orders/{order_id}/deliver")
async def deliver_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    ORDERS[order_id]["status"] = "delivered"
    ORDERS[order_id]["fulfillment"]["expectations"][0]["status"] = "delivered"
    return JSONResponse(ORDERS[order_id])


@app.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    ORDERS[order_id]["status"] = "canceled"
    return JSONResponse(ORDERS[order_id])


@app.post("/orders/{order_id}/return")
async def return_order(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    ORDERS[order_id]["status"] = "returned"
    return JSONResponse(ORDERS[order_id])


@app.post("/testing/simulate-shipping/{order_id}")
async def simulate_shipping(order_id: str):
    if order_id not in ORDERS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    ORDERS[order_id]["status"] = "shipped"
    return JSONResponse(ORDERS[order_id])


# ======================================================================
# Helper: create + complete a checkout, return order_id
# ======================================================================


async def quick_checkout(
    client: httpx.AsyncClient,
    tracker: UCPAnalyticsTracker,
    fulfillment_type: str = "shipping",
) -> str:
    """Create and complete a checkout, return order_id."""
    resp = await client.post(
        "/checkout-sessions",
        json={
            "line_items": [{"item": {"id": "bouquet"}, "quantity": 1}],
            "fulfillment_type": fulfillment_type,
        },
    )
    checkout = resp.json()
    session_id = checkout["id"]
    await tracker.record_http(
        method="POST",
        path="/checkout-sessions",
        status_code=resp.status_code,
        response_body=checkout,
    )

    resp = await client.post(
        f"/checkout-sessions/{session_id}/complete",
        json={
            "payment_data": {
                "handler_id": "mock_handler",
                "type": "card",
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
    return completed["order_id"]


# ======================================================================
# Order lifecycle runner
# ======================================================================


async def run_order_demo(tracker: UCPAnalyticsTracker):
    print("\n" + "=" * 70)
    print("  UCP ORDER LIFECYCLE DEMO")
    print("=" * 70)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8196") as client:
        # --- Flow 1: Happy path to delivery ---
        print("\n-- Flow 1: Happy Path (Created -> Shipped -> Delivered) --")
        order_id = await quick_checkout(client, tracker)
        print(f"   Order created: {order_id}")

        # Ship
        resp = await client.post(f"/orders/{order_id}/ship")
        await tracker.record_http(
            method="GET",
            path=f"/orders/{order_id}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Shipped: status={resp.json()['status']}")

        # Deliver
        resp = await client.post(f"/orders/{order_id}/deliver")
        await tracker.record_http(
            method="GET",
            path=f"/orders/{order_id}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Delivered: status={resp.json()['status']}")

        # --- Flow 2: Order cancellation ---
        print("\n-- Flow 2: Order Cancellation --")
        order_id2 = await quick_checkout(client, tracker)
        print(f"   Order created: {order_id2}")

        resp = await client.post(f"/orders/{order_id2}/cancel")
        await tracker.record_http(
            method="GET",
            path=f"/orders/{order_id2}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Canceled: status={resp.json()['status']}")

        # --- Flow 3: Return after delivery ---
        print("\n-- Flow 3: Return After Delivery --")
        order_id3 = await quick_checkout(client, tracker)
        print(f"   Order created: {order_id3}")

        resp = await client.post(f"/orders/{order_id3}/ship")
        await tracker.record_http(
            method="GET",
            path=f"/orders/{order_id3}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Shipped: status={resp.json()['status']}")

        resp = await client.post(f"/orders/{order_id3}/deliver")
        await tracker.record_http(
            method="GET",
            path=f"/orders/{order_id3}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Delivered: status={resp.json()['status']}")

        resp = await client.post(f"/orders/{order_id3}/return")
        await tracker.record_http(
            method="GET",
            path=f"/orders/{order_id3}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Returned: status={resp.json()['status']}")

        # --- Flow 4: Fulfillment variants ---
        print("\n-- Flow 4: Fulfillment Variants --")
        for ff_type in ["shipping", "pickup", "digital"]:
            oid = await quick_checkout(client, tracker, fulfillment_type=ff_type)
            resp = await client.get(f"/orders/{oid}")
            order = resp.json()
            await tracker.record_http(
                method="GET",
                path=f"/orders/{oid}",
                status_code=resp.status_code,
                response_body=order,
            )
            ff = order["fulfillment"]["expectations"][0]["method_type"]
            print(f"   {ff_type}: order={oid}, fulfillment={ff}")


# ======================================================================
# Main
# ======================================================================


async def main():
    tracker = create_tracker("order_lifecycle_demo")

    config = uvicorn.Config(app, host="127.0.0.1", port=8196, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                await c.get("http://127.0.0.1:8196/orders/ping")
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.1)

    try:
        await run_order_demo(tracker)

        print("\n   Flushing events to BigQuery...")
        await tracker.close()

        await verify_bigquery(
            "order_lifecycle_demo", "Order Lifecycle Analytics Report"
        )
    finally:
        server.should_exit = True
        await server_task

    print("\nDemo complete.")


if __name__ == "__main__":
    asyncio.run(main())
