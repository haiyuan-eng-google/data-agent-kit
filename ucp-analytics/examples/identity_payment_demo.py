#!/usr/bin/env python3
"""
Identity & Payment Demo (BigQuery)
====================================

Exercises identity linking and payment negotiation flows:
  1. OAuth identity linking — initiate -> callback -> linked
  2. Identity revocation — revoke link
  3. Payment handler negotiation — compute intersection of platform + merchant
  4. Payment instrument selection — buyer picks from available instruments
  5. Payment failure + success — fail_token -> payment_failed,
     success_token -> payment_completed

Uses UCP SDK Pydantic models for typed request construction
and UCPAnalyticsTracker -> BigQuery for analytics.

Event types covered:
  IDENTITY_LINK_INITIATED, IDENTITY_LINK_COMPLETED, IDENTITY_LINK_REVOKED,
  PAYMENT_HANDLER_NEGOTIATED, PAYMENT_INSTRUMENT_SELECTED,
  PAYMENT_COMPLETED, PAYMENT_FAILED, PROFILE_DISCOVERED,
  CHECKOUT_SESSION_CREATED, CHECKOUT_SESSION_COMPLETED

Run:
    uv run python examples/identity_payment_demo.py

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

# UCP SDK models for typed request construction
from ucp_sdk.models.schemas.shopping.types.card_payment_instrument import (
    CardPaymentInstrument,
)

from ucp_analytics import UCPAnalyticsTracker
from ucp_analytics.events import UCPEvent, UCPEventType

sys.path.insert(0, os.path.dirname(__file__))
from _demo_utils import create_tracker, verify_bigquery

# ======================================================================
# Mini UCP Server with identity + payment endpoints
# ======================================================================

SESSIONS: Dict[str, dict] = {}
IDENTITY_LINKS: Dict[str, dict] = {}
UCP_VERSION = "2026-01-11"

MERCHANT_HANDLERS = [
    {
        "id": "com.stripe",
        "name": "Stripe",
        "version": UCP_VERSION,
        "types": ["card", "bank"],
    },
    {
        "id": "com.google.pay",
        "name": "Google Pay",
        "version": UCP_VERSION,
        "types": ["wallet"],
    },
    {"id": "com.paypal", "name": "PayPal", "version": UCP_VERSION, "types": ["wallet"]},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="UCP Identity & Payment Demo", lifespan=lifespan)


@app.get("/.well-known/ucp")
async def discovery():
    return {
        "ucp": {
            "version": UCP_VERSION,
            "capabilities": [
                {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
                {"name": "dev.ucp.identity", "version": UCP_VERSION},
            ],
        },
        "payment": {"handlers": MERCHANT_HANDLERS},
    }


# --- Identity endpoints ---


@app.post("/identity")
async def initiate_identity(request: Request):
    body = await request.json()
    link_id = f"link_{uuid.uuid4().hex[:10]}"
    link = {
        "id": link_id,
        "status": "pending",
        "provider": body.get("provider", "google"),
        "scope": body.get("scope", "openid email profile"),
        "authorize_url": f"https://accounts.google.com/o/oauth2/auth?state={link_id}",
    }
    IDENTITY_LINKS[link_id] = link
    return JSONResponse(link, status_code=201)


@app.get("/identity/callback")
async def identity_callback(request: Request):
    # Simulate OAuth callback completing the link
    state = request.query_params.get("state", "")
    if state in IDENTITY_LINKS:
        IDENTITY_LINKS[state]["status"] = "linked"
        IDENTITY_LINKS[state]["identity"] = {
            "provider": IDENTITY_LINKS[state]["provider"],
            "scope": IDENTITY_LINKS[state]["scope"],
            "external_id": f"user_{uuid.uuid4().hex[:8]}",
        }
        return JSONResponse(IDENTITY_LINKS[state])
    return JSONResponse(
        {"provider": "google", "scope": "openid email", "status": "linked"}
    )


@app.post("/identity/revoke")
async def revoke_identity(request: Request):
    body = await request.json()
    link_id = body.get("link_id", "")
    if link_id in IDENTITY_LINKS:
        IDENTITY_LINKS[link_id]["status"] = "revoked"
        return JSONResponse(IDENTITY_LINKS[link_id])
    return JSONResponse({"status": "revoked", "link_id": link_id})


# --- Payment endpoints ---


@app.post("/checkout-sessions")
async def create_checkout(request: Request):
    body = await request.json()
    session_id = f"chk_{uuid.uuid4().hex[:12]}"
    session = {
        "id": session_id,
        "status": "incomplete",
        "currency": "USD",
        "line_items": body.get("line_items", []),
        "totals": [
            {"type": "subtotal", "amount": 5999},
            {"type": "total", "amount": 5999},
        ],
        "payment": {
            "handlers": MERCHANT_HANDLERS,
            "instruments": [],
        },
    }
    SESSIONS[session_id] = session
    return JSONResponse(session, status_code=201)


@app.post("/checkout-sessions/{session_id}/select-instrument")
async def select_instrument(session_id: str, request: Request):
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    body = await request.json()
    session = SESSIONS[session_id]
    instrument = {
        "id": f"instr_{uuid.uuid4().hex[:8]}",
        "handler_id": body.get("handler_id", "com.stripe"),
        "type": body.get("type", "card"),
        "brand": body.get("brand", "visa"),
    }
    session["payment"]["instruments"] = [instrument]
    session["status"] = "ready_for_complete"
    SESSIONS[session_id] = session
    return JSONResponse(session)


@app.post("/checkout-sessions/{session_id}/complete")
async def complete_checkout(session_id: str, request: Request):
    if session_id not in SESSIONS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    body = await request.json()
    session = SESSIONS[session_id]
    payment_data = body.get("payment_data", {})
    token = payment_data.get("credential", {}).get("token", "")

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

    session["status"] = "completed"
    session["order_id"] = f"order_{uuid.uuid4().hex[:10]}"
    session["payment_data"] = payment_data
    SESSIONS[session_id] = session
    return JSONResponse(session)


# ======================================================================
# Demo runner
# ======================================================================


async def run_identity_payment_demo(tracker: UCPAnalyticsTracker):
    print("\n" + "=" * 70)
    print("  UCP IDENTITY & PAYMENT DEMO")
    print("=" * 70)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8194") as client:
        # --- Discovery ---
        print("\n-- Discovery --")
        resp = await client.get("/.well-known/ucp")
        profile = resp.json()
        await tracker.record_http(
            method="GET",
            path="/.well-known/ucp",
            status_code=resp.status_code,
            response_body=profile,
        )
        print(
            f"   Merchant handlers: {[h['id'] for h in profile['payment']['handlers']]}"
        )

        # --- Flow 1: OAuth identity linking ---
        print("\n-- Flow 1: OAuth Identity Linking --")

        # Initiate
        resp = await client.post(
            "/identity",
            json={"provider": "google", "scope": "openid email profile"},
        )
        link = resp.json()
        link_id = link["id"]
        await tracker.record_http(
            method="POST",
            path="/identity",
            status_code=resp.status_code,
            response_body=link,
        )
        print(f"   Initiated: link_id={link_id}")
        print(f"   Provider: {link['provider']}, Scope: {link['scope']}")
        print(f"   Authorize URL: {link['authorize_url']}")

        # Callback (simulated)
        resp = await client.get(f"/identity/callback?state={link_id}")
        callback = resp.json()
        await tracker.record_http(
            method="GET",
            path="/identity/callback",
            status_code=resp.status_code,
            response_body=callback,
        )
        print(f"   Callback: status={callback['status']}")

        # --- Flow 2: Identity revocation ---
        print("\n-- Flow 2: Identity Revocation --")
        resp = await client.post("/identity/revoke", json={"link_id": link_id})
        revoked = resp.json()
        await tracker.record_http(
            method="POST",
            path="/identity/revoke",
            status_code=resp.status_code,
            response_body=revoked,
        )
        print(f"   Revoked: status={revoked['status']}")

        # --- Flow 3: Payment handler negotiation ---
        print("\n-- Flow 3: Payment Handler Negotiation --")
        platform_handlers = ["com.stripe", "com.google.pay", "com.apple.pay"]
        merchant_handler_ids = [h["id"] for h in MERCHANT_HANDLERS]
        common = set(platform_handlers) & set(merchant_handler_ids)
        # Record as a manually constructed UCPEvent
        event = UCPEvent(
            event_type=UCPEventType.PAYMENT_HANDLER_NEGOTIATED.value,
            app_name="identity_payment_demo",
            merchant_host="localhost",
            payment_handler_id=sorted(common)[0] if common else None,
        )
        await tracker.record_event(event)
        print(f"   Platform: {platform_handlers}")
        print(f"   Merchant: {merchant_handler_ids}")
        print(f"   Negotiated: {sorted(common)}")

        # --- Flow 4: Payment instrument selection ---
        print("\n-- Flow 4: Payment Instrument Selection --")
        resp = await client.post(
            "/checkout-sessions",
            json={"line_items": [{"item": {"id": "bouquet"}, "quantity": 1}]},
        )
        checkout = resp.json()
        session_id = checkout["id"]
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=resp.status_code,
            response_body=checkout,
        )

        # Select instrument (using SDK CardPaymentInstrument for type context)
        card = CardPaymentInstrument(
            id="instr_demo",
            handler_id="com.stripe",
            type="card",
            brand="visa",
            last_digits="4242",
        )
        resp = await client.post(
            f"/checkout-sessions/{session_id}/select-instrument",
            json=card.model_dump(exclude_none=True),
        )
        selected = resp.json()
        instrument = selected["payment"]["instruments"][0]
        # Record instrument selection as a UCPEvent
        event = UCPEvent(
            event_type=UCPEventType.PAYMENT_INSTRUMENT_SELECTED.value,
            app_name="identity_payment_demo",
            merchant_host="localhost",
            payment_handler_id=instrument["handler_id"],
            payment_instrument_type=instrument["type"],
            payment_brand=instrument["brand"],
        )
        await tracker.record_event(event)
        handler = instrument["handler_id"]
        itype = instrument["type"]
        brand = instrument["brand"]
        print(f"   Selected: {handler} / {itype} / {brand}")

        # --- Flow 5: Payment failure + success ---
        print("\n-- Flow 5: Payment Failure + Success --")

        # Fail
        resp = await client.post(
            f"/checkout-sessions/{session_id}/complete",
            json={"payment_data": {"credential": {"token": "fail_token"}}},
        )
        failed = resp.json()
        event = UCPEvent(
            event_type=UCPEventType.PAYMENT_FAILED.value,
            app_name="identity_payment_demo",
            merchant_host="localhost",
            http_status_code=resp.status_code,
            error_code="payment_declined",
            error_message="Card declined",
        )
        await tracker.record_event(event)
        print(f"   Payment failed: {failed.get('messages', [{}])[0].get('content')}")

        # Success (using SDK Buyer model for context)
        resp = await client.post(
            f"/checkout-sessions/{session_id}/complete",
            json={
                "payment_data": {
                    "handler_id": "com.stripe",
                    "type": "card",
                    "brand": "visa",
                    "credential": {"token": "success_token"},
                }
            },
        )
        completed = resp.json()
        event = UCPEvent(
            event_type=UCPEventType.PAYMENT_COMPLETED.value,
            app_name="identity_payment_demo",
            merchant_host="localhost",
            payment_handler_id="com.stripe",
            payment_instrument_type="card",
            payment_brand="visa",
        )
        await tracker.record_event(event)
        await tracker.record_http(
            method="POST",
            path=f"/checkout-sessions/{session_id}/complete",
            status_code=resp.status_code,
            response_body=completed,
        )
        print(f"   Payment completed: order_id={completed.get('order_id')}")


# ======================================================================
# Main
# ======================================================================


async def main():
    tracker = create_tracker("identity_payment_demo")

    config = uvicorn.Config(app, host="127.0.0.1", port=8194, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                await c.get("http://127.0.0.1:8194/.well-known/ucp")
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.1)

    try:
        await run_identity_payment_demo(tracker)

        print("\n   Flushing events to BigQuery...")
        await tracker.close()

        await verify_bigquery(
            "identity_payment_demo", "Identity & Payment Analytics Report"
        )
    finally:
        server.should_exit = True
        await server_task

    print("\nDemo complete.")


if __name__ == "__main__":
    asyncio.run(main())
