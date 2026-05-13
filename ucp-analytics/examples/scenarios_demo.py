#!/usr/bin/env python3
"""
Scenarios Demo — Errors, Cancellation, Escalation (BigQuery)
==============================================================

Exercises error paths and edge cases in UCP checkout:
  1. Payment failure + retry (402 -> 200)
  2. Fraud block (403)
  3. Out of stock (400)
  4. Checkout cancellation
  5. Escalation + recovery (requires_escalation -> poll -> complete)
  6. 404 Not Found
  7. Idempotency conflict (409)

Uses UCP SDK Pydantic models for typed request construction
and UCPAnalyticsTracker -> BigQuery for analytics.

Event types covered:
  CHECKOUT_SESSION_CREATED, CHECKOUT_SESSION_UPDATED,
  CHECKOUT_SESSION_COMPLETED, CHECKOUT_SESSION_CANCELED,
  CHECKOUT_ESCALATION, CHECKOUT_SESSION_GET, ERROR

Run:
    uv run python examples/scenarios_demo.py

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
from ucp_sdk.models.schemas.shopping.types.buyer import Buyer
from ucp_sdk.models.schemas.shopping.types.item_create_req import ItemCreateRequest
from ucp_sdk.models.schemas.shopping.types.line_item_create_req import (
    LineItemCreateRequest,
)

from ucp_analytics import UCPAnalyticsTracker

sys.path.insert(0, os.path.dirname(__file__))
from _demo_utils import create_tracker, verify_bigquery

# ======================================================================
# Mini UCP Server with error scenarios
# ======================================================================

SESSIONS: Dict[str, dict] = {}
IDEMPOTENCY_KEYS: Dict[str, str] = {}  # key -> session_id
UCP_VERSION = "2026-01-11"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="UCP Scenarios Demo Server", lifespan=lifespan)


@app.get("/.well-known/ucp")
async def discovery():
    return {
        "ucp": {
            "version": UCP_VERSION,
            "capabilities": [
                {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
            ],
        },
        "payment": {
            "handlers": [
                {"id": "mock_handler", "name": "Mock Pay", "version": UCP_VERSION}
            ],
        },
    }


@app.post("/checkout-sessions")
async def create_checkout(request: Request):
    body = await request.json()

    # Idempotency check
    idem_key = request.headers.get("idempotency-key", "")
    if idem_key and idem_key in IDEMPOTENCY_KEYS:
        return JSONResponse(
            {"error": "conflict", "message": "Duplicate idempotency key"},
            status_code=409,
        )

    # Out of stock check
    for item in body.get("line_items", []):
        item_id = item.get("item", {}).get("id", "")
        if item_id == "out_of_stock_item":
            return JSONResponse(
                {
                    "messages": [
                        {
                            "type": "error",
                            "code": "out_of_stock",
                            "content": f"Item {item_id} is out of stock",
                            "severity": "blocking",
                        }
                    ]
                },
                status_code=400,
            )

    session_id = f"chk_{uuid.uuid4().hex[:12]}"
    session = {
        "id": session_id,
        "status": "incomplete",
        "currency": "USD",
        "line_items": body.get("line_items", []),
        "totals": [
            {"type": "subtotal", "amount": 2999},
            {"type": "total", "amount": 2999},
        ],
        "payment": {
            "handlers": [
                {"id": "mock_handler", "name": "Mock Pay", "version": UCP_VERSION}
            ],
        },
    }
    SESSIONS[session_id] = session
    if idem_key:
        IDEMPOTENCY_KEYS[idem_key] = session_id
    return JSONResponse(session, status_code=201)


@app.put("/checkout-sessions/{session_id}")
async def update_checkout(session_id: str, request: Request):
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    body = await request.json()
    session = SESSIONS[session_id]
    if "buyer" in body:
        session["buyer"] = body["buyer"]
    session["status"] = "ready_for_complete"
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
    payment_data = body.get("payment_data", {})
    token = payment_data.get("credential", {}).get("token", "")

    # Payment failure
    if token == "fail_token":
        session["status"] = "incomplete"
        session["messages"] = [
            {
                "type": "error",
                "code": "payment_declined",
                "content": "Card declined",
                "severity": "recoverable",
            }
        ]
        SESSIONS[session_id] = session
        return JSONResponse(session, status_code=402)

    # Fraud block
    if token == "fraud_token":
        session["status"] = "canceled"
        session["messages"] = [
            {
                "type": "error",
                "code": "fraud_detected",
                "content": "Transaction blocked for fraud",
                "severity": "blocking",
            }
        ]
        SESSIONS[session_id] = session
        return JSONResponse(session, status_code=403)

    # Escalation trigger
    if token == "escalate_token":
        session["status"] = "requires_escalation"
        session["continue_url"] = "https://shop.example.com/escalate"
        SESSIONS[session_id] = session
        return JSONResponse(session)

    # Success
    order_id = f"order_{uuid.uuid4().hex[:10]}"
    session["status"] = "completed"
    session["order_id"] = order_id
    session["payment_data"] = payment_data
    SESSIONS[session_id] = session
    return JSONResponse(session)


@app.post("/checkout-sessions/{session_id}/cancel")
async def cancel_checkout(session_id: str):
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    SESSIONS[session_id]["status"] = "canceled"
    return JSONResponse(SESSIONS[session_id])


# ======================================================================
# Scenario runner
# ======================================================================


def _make_line_items(item_id: str, qty: int = 1) -> list[dict]:
    """Build a line_items list using SDK models."""
    li = LineItemCreateRequest(
        item=ItemCreateRequest(id=item_id),
        quantity=qty,
    )
    return [li.model_dump(exclude_none=True)]


async def run_scenarios(tracker: UCPAnalyticsTracker):
    print("\n" + "=" * 70)
    print("  UCP SCENARIOS DEMO — Errors, Cancellation, Escalation")
    print("=" * 70)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8198") as client:
        # --- Scenario 1: Payment failure + retry ---
        print("\n-- Scenario 1: Payment Failure + Retry --")
        start = time.monotonic()
        resp = await client.post(
            "/checkout-sessions",
            json={"line_items": _make_line_items("roses")},
        )
        latency = round((time.monotonic() - start) * 1000, 2)
        checkout = resp.json()
        session_id = checkout["id"]
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=resp.status_code,
            response_body=checkout,
            latency_ms=latency,
        )
        print(f"   Created: {session_id}")

        # Update to ready (using SDK Buyer model)
        buyer = Buyer(email="test@example.com")
        resp = await client.put(
            f"/checkout-sessions/{session_id}",
            json={"buyer": buyer.model_dump(exclude_none=True)},
        )
        await tracker.record_http(
            method="PUT",
            path=f"/checkout-sessions/{session_id}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )

        # Fail payment
        start = time.monotonic()
        resp = await client.post(
            f"/checkout-sessions/{session_id}/complete",
            json={"payment_data": {"credential": {"token": "fail_token"}}},
        )
        latency = round((time.monotonic() - start) * 1000, 2)
        await tracker.record_http(
            method="POST",
            path=f"/checkout-sessions/{session_id}/complete",
            status_code=resp.status_code,
            response_body=resp.json(),
            latency_ms=latency,
        )
        msg = resp.json().get("messages", [{}])[0].get("content")
        print(f"   Payment failed (402): {msg}")

        # Retry with success
        start = time.monotonic()
        resp = await client.post(
            f"/checkout-sessions/{session_id}/complete",
            json={
                "payment_data": {
                    "handler_id": "mock_handler",
                    "type": "card",
                    "credential": {"token": "success_token"},
                }
            },
        )
        latency = round((time.monotonic() - start) * 1000, 2)
        await tracker.record_http(
            method="POST",
            path=f"/checkout-sessions/{session_id}/complete",
            status_code=resp.status_code,
            response_body=resp.json(),
            latency_ms=latency,
        )
        print(f"   Retry succeeded: status={resp.json()['status']}")

        # --- Scenario 2: Fraud block ---
        print("\n-- Scenario 2: Fraud Block --")
        resp = await client.post(
            "/checkout-sessions",
            json={"line_items": _make_line_items("roses")},
        )
        checkout = resp.json()
        sid2 = checkout["id"]
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=resp.status_code,
            response_body=checkout,
        )

        resp = await client.put(
            f"/checkout-sessions/{sid2}",
            json={
                "buyer": Buyer(email="fraud@example.com").model_dump(exclude_none=True)
            },
        )
        await tracker.record_http(
            method="PUT",
            path=f"/checkout-sessions/{sid2}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )

        resp = await client.post(
            f"/checkout-sessions/{sid2}/complete",
            json={"payment_data": {"credential": {"token": "fraud_token"}}},
        )
        await tracker.record_http(
            method="POST",
            path=f"/checkout-sessions/{sid2}/complete",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Fraud blocked (403): {resp.json()['messages'][0]['content']}")

        # --- Scenario 3: Out of stock ---
        print("\n-- Scenario 3: Out of Stock --")
        resp = await client.post(
            "/checkout-sessions",
            json={"line_items": _make_line_items("out_of_stock_item")},
        )
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Out of stock (400): {resp.json()['messages'][0]['content']}")

        # --- Scenario 4: Checkout cancellation ---
        print("\n-- Scenario 4: Checkout Cancellation --")
        resp = await client.post(
            "/checkout-sessions",
            json={"line_items": _make_line_items("tulips")},
        )
        checkout = resp.json()
        sid4 = checkout["id"]
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=resp.status_code,
            response_body=checkout,
        )

        resp = await client.post(f"/checkout-sessions/{sid4}/cancel")
        await tracker.record_http(
            method="POST",
            path=f"/checkout-sessions/{sid4}/cancel",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Canceled: status={resp.json()['status']}")

        # --- Scenario 5: Escalation + recovery ---
        print("\n-- Scenario 5: Escalation + Recovery --")
        resp = await client.post(
            "/checkout-sessions",
            json={"line_items": _make_line_items("premium_bouquet")},
        )
        checkout = resp.json()
        sid5 = checkout["id"]
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=resp.status_code,
            response_body=checkout,
        )

        resp = await client.put(
            f"/checkout-sessions/{sid5}",
            json={
                "buyer": Buyer(email="vip@example.com").model_dump(exclude_none=True)
            },
        )
        await tracker.record_http(
            method="PUT",
            path=f"/checkout-sessions/{sid5}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )

        # Trigger escalation
        resp = await client.post(
            f"/checkout-sessions/{sid5}/complete",
            json={"payment_data": {"credential": {"token": "escalate_token"}}},
        )
        await tracker.record_http(
            method="POST",
            path=f"/checkout-sessions/{sid5}/complete",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Escalated: continue_url={resp.json().get('continue_url')}")

        # Poll with GET
        resp = await client.get(f"/checkout-sessions/{sid5}")
        await tracker.record_http(
            method="GET",
            path=f"/checkout-sessions/{sid5}",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Polled: status={resp.json()['status']}")

        # Resolve escalation (complete with good token)
        SESSIONS[sid5]["status"] = "ready_for_complete"
        resp = await client.post(
            f"/checkout-sessions/{sid5}/complete",
            json={
                "payment_data": {
                    "handler_id": "mock_handler",
                    "type": "card",
                    "credential": {"token": "success_token"},
                }
            },
        )
        await tracker.record_http(
            method="POST",
            path=f"/checkout-sessions/{sid5}/complete",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   Resolved: status={resp.json()['status']}")

        # --- Scenario 6: 404 Not Found ---
        print("\n-- Scenario 6: 404 Not Found --")
        resp = await client.get("/checkout-sessions/nonexistent_session")
        await tracker.record_http(
            method="GET",
            path="/checkout-sessions/nonexistent_session",
            status_code=resp.status_code,
            response_body=resp.json(),
        )
        print(f"   404: {resp.json()}")

        # --- Scenario 7: Idempotency conflict ---
        print("\n-- Scenario 7: Idempotency Conflict --")
        idem_key = str(uuid.uuid4())
        resp = await client.post(
            "/checkout-sessions",
            json={"line_items": _make_line_items("roses")},
            headers={"idempotency-key": idem_key},
        )
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=resp.status_code,
            response_body=resp.json(),
            request_headers={"idempotency-key": idem_key},
        )
        print(f"   First request: {resp.status_code}")

        resp = await client.post(
            "/checkout-sessions",
            json={"line_items": _make_line_items("roses")},
            headers={"idempotency-key": idem_key},
        )
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=resp.status_code,
            response_body=resp.json(),
            request_headers={"idempotency-key": idem_key},
        )
        print(f"   Duplicate request: {resp.status_code} (conflict)")


# ======================================================================
# Main
# ======================================================================


async def main():
    tracker = create_tracker("scenarios_demo")

    config = uvicorn.Config(app, host="127.0.0.1", port=8198, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                await c.get("http://127.0.0.1:8198/.well-known/ucp")
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.1)

    try:
        await run_scenarios(tracker)

        print("\n   Flushing events to BigQuery...")
        await tracker.close()

        await verify_bigquery("scenarios_demo", "Scenarios Analytics Report")
    finally:
        server.should_exit = True
        await server_task

    print("\nDemo complete.")


if __name__ == "__main__":
    asyncio.run(main())
