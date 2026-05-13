#!/usr/bin/env python3
"""
Transport Demo — REST vs MCP vs A2A (BigQuery)
================================================

Simulates the same shopping flow across all 3 UCP transports:
  1. REST — standard HTTP calls
  2. MCP — JSON-RPC tools/call with tool names (create_checkout, etc.)
  3. A2A — JSON-RPC tasks/send with DataParts keyed by a2a.ucp.*

Uses classify_jsonrpc() for MCP/A2A and compares event types across
all transports.

Uses UCP SDK Pydantic models for type context
and UCPAnalyticsTracker -> BigQuery for analytics.

Event types covered:
  PROFILE_DISCOVERED, CHECKOUT_SESSION_CREATED, CHECKOUT_SESSION_UPDATED,
  CHECKOUT_SESSION_COMPLETED, CAPABILITY_NEGOTIATED

Run:
    uv run python examples/transport_demo.py

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

# UCP SDK models for type context
from ucp_sdk.models.schemas.shopping.types.buyer import Buyer

from ucp_analytics import UCPAnalyticsTracker

sys.path.insert(0, os.path.dirname(__file__))
from _demo_utils import create_tracker, verify_bigquery

# ======================================================================
# Mini UCP Server (shared by all transports)
# ======================================================================

SESSIONS: Dict[str, dict] = {}
UCP_VERSION = "2026-01-11"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="UCP Transport Demo Server", lifespan=lifespan)


@app.get("/.well-known/ucp")
async def discovery():
    return {
        "ucp": {
            "version": UCP_VERSION,
            "capabilities": [
                {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
                {"name": "dev.ucp.shopping.fulfillment", "version": UCP_VERSION},
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
    session_id = f"chk_{uuid.uuid4().hex[:12]}"
    # Include UCP metadata envelope (ResponseCheckout)
    session = {
        "ucp": {
            "version": UCP_VERSION,
            "capabilities": [
                {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
            ],
        },
        "id": session_id,
        "status": "incomplete",
        "currency": "USD",
        "line_items": body.get("line_items", []),
        "totals": [
            {"type": "subtotal", "amount": 2999},
            {"type": "total", "amount": 2999},
        ],
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
    session["status"] = "ready_for_complete"
    SESSIONS[session_id] = session
    return JSONResponse(session)


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
# Transport simulators
# ======================================================================


async def run_rest_flow(
    client: httpx.AsyncClient, tracker: UCPAnalyticsTracker
) -> list:
    """Standard REST transport."""
    events = []

    # Discovery
    start = time.monotonic()
    resp = await client.get("/.well-known/ucp")
    latency = round((time.monotonic() - start) * 1000, 2)
    row = await tracker.record_http(
        method="GET",
        path="/.well-known/ucp",
        status_code=resp.status_code,
        response_body=resp.json(),
        latency_ms=latency,
    )
    events.append(row.event_type)

    # Create checkout
    resp = await client.post(
        "/checkout-sessions",
        json={"line_items": [{"item": {"id": "roses"}, "quantity": 1}]},
    )
    checkout = resp.json()
    session_id = checkout["id"]
    row = await tracker.record_http(
        method="POST",
        path="/checkout-sessions",
        status_code=resp.status_code,
        response_body=checkout,
    )
    events.append(row.event_type)

    # Update (using SDK Buyer model)
    buyer = Buyer(email="test@example.com")
    resp = await client.put(
        f"/checkout-sessions/{session_id}",
        json={"buyer": buyer.model_dump(exclude_none=True)},
    )
    row = await tracker.record_http(
        method="PUT",
        path=f"/checkout-sessions/{session_id}",
        status_code=resp.status_code,
        response_body=resp.json(),
    )
    events.append(row.event_type)

    # Complete
    resp = await client.post(
        f"/checkout-sessions/{session_id}/complete",
        json={"payment_data": {"handler_id": "mock_handler", "type": "card"}},
    )
    row = await tracker.record_http(
        method="POST",
        path=f"/checkout-sessions/{session_id}/complete",
        status_code=resp.status_code,
        response_body=resp.json(),
    )
    events.append(row.event_type)

    return events


async def run_mcp_flow(client: httpx.AsyncClient, tracker: UCPAnalyticsTracker) -> list:
    """MCP transport — uses classify_jsonrpc with MCP tool names."""
    events = []

    # Discovery via MCP tool
    start = time.monotonic()
    resp = await client.get("/.well-known/ucp")
    latency = round((time.monotonic() - start) * 1000, 2)
    row = await tracker.record_jsonrpc(
        tool_name="discover_merchant",
        status_code=200,
        response_body=resp.json(),
        latency_ms=latency,
        transport="mcp",
    )
    events.append(row.event_type)

    # Create checkout
    resp = await client.post(
        "/checkout-sessions",
        json={"line_items": [{"item": {"id": "roses"}, "quantity": 1}]},
    )
    checkout = resp.json()
    session_id = checkout["id"]
    row = await tracker.record_jsonrpc(
        tool_name="create_checkout",
        status_code=201,
        response_body=checkout,
        transport="mcp",
    )
    events.append(row.event_type)

    # Update
    resp = await client.put(
        f"/checkout-sessions/{session_id}",
        json={"buyer": Buyer(email="mcp@example.com").model_dump(exclude_none=True)},
    )
    row = await tracker.record_jsonrpc(
        tool_name="update_checkout",
        status_code=200,
        response_body=resp.json(),
        transport="mcp",
    )
    events.append(row.event_type)

    # Complete
    resp = await client.post(
        f"/checkout-sessions/{session_id}/complete",
        json={"payment_data": {"handler_id": "mock_handler", "type": "card"}},
    )
    row = await tracker.record_jsonrpc(
        tool_name="complete_checkout",
        status_code=200,
        response_body=resp.json(),
        transport="mcp",
    )
    events.append(row.event_type)

    return events


async def run_a2a_flow(client: httpx.AsyncClient, tracker: UCPAnalyticsTracker) -> list:
    """A2A transport — uses classify_jsonrpc with a2a.ucp.* action names."""
    events = []

    # Capability negotiation (A2A extension)
    row = await tracker.record_jsonrpc(
        tool_name="a2a.ucp.capability.negotiate",
        status_code=200,
        response_body={
            "ucp": {
                "version": UCP_VERSION,
                "capabilities": [
                    {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
                ],
            }
        },
        transport="a2a",
    )
    events.append(row.event_type)

    # Discovery
    resp = await client.get("/.well-known/ucp")
    row = await tracker.record_jsonrpc(
        tool_name="a2a.ucp.discover",
        status_code=200,
        response_body=resp.json(),
        transport="a2a",
    )
    events.append(row.event_type)

    # Create checkout
    resp = await client.post(
        "/checkout-sessions",
        json={"line_items": [{"item": {"id": "roses"}, "quantity": 1}]},
    )
    checkout = resp.json()
    session_id = checkout["id"]
    row = await tracker.record_jsonrpc(
        tool_name="a2a.ucp.checkout.create",
        status_code=201,
        response_body=checkout,
        transport="a2a",
    )
    events.append(row.event_type)

    # Update
    resp = await client.put(
        f"/checkout-sessions/{session_id}",
        json={"buyer": Buyer(email="a2a@example.com").model_dump(exclude_none=True)},
    )
    row = await tracker.record_jsonrpc(
        tool_name="a2a.ucp.checkout.update",
        status_code=200,
        response_body=resp.json(),
        transport="a2a",
    )
    events.append(row.event_type)

    # Complete
    resp = await client.post(
        f"/checkout-sessions/{session_id}/complete",
        json={"payment_data": {"handler_id": "mock_handler", "type": "card"}},
    )
    row = await tracker.record_jsonrpc(
        tool_name="a2a.ucp.checkout.complete",
        status_code=200,
        response_body=resp.json(),
        transport="a2a",
    )
    events.append(row.event_type)

    return events


# ======================================================================
# Main runner
# ======================================================================


async def run_transport_demo(tracker: UCPAnalyticsTracker):
    print("\n" + "=" * 70)
    print("  UCP TRANSPORT DEMO — REST vs MCP vs A2A")
    print("=" * 70)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8195") as client:
        # REST flow
        print("\n-- REST Transport --")
        rest_events = await run_rest_flow(client, tracker)
        for e in rest_events:
            print(f"   {e}")

        # MCP flow
        print("\n-- MCP Transport --")
        mcp_events = await run_mcp_flow(client, tracker)
        for e in mcp_events:
            print(f"   {e}")

        # A2A flow
        print("\n-- A2A Transport --")
        a2a_events = await run_a2a_flow(client, tracker)
        for e in a2a_events:
            print(f"   {e}")

    # Comparison
    print("\n-- Transport Comparison --")
    print(f"   {'Step':<25} {'REST':<30} {'MCP':<30} {'A2A':<30}")
    print("   " + "-" * 115)

    # Align by padding shorter lists
    max_len = max(len(rest_events), len(mcp_events), len(a2a_events))
    rest_pad = rest_events + [""] * (max_len - len(rest_events))
    mcp_pad = mcp_events + [""] * (max_len - len(mcp_events))
    a2a_pad = a2a_events + [""] * (max_len - len(a2a_events))

    steps = [
        "capability_negotiate",
        "discovery",
        "create_checkout",
        "update_checkout",
        "complete_checkout",
    ]
    for i in range(max_len):
        step = steps[i] if i < len(steps) else f"step_{i}"
        print(f"   {step:<25} {rest_pad[i]:<30} {mcp_pad[i]:<30} {a2a_pad[i]:<30}")


async def main():
    tracker = create_tracker("transport_demo")

    config = uvicorn.Config(app, host="127.0.0.1", port=8195, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                await c.get("http://127.0.0.1:8195/.well-known/ucp")
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.1)

    try:
        await run_transport_demo(tracker)

        print("\n   Flushing events to BigQuery...")
        await tracker.close()

        await verify_bigquery("transport_demo", "Transport Analytics Report")
    finally:
        server.should_exit = True
        await server_task

    print("\nDemo complete.")


if __name__ == "__main__":
    asyncio.run(main())
