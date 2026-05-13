# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ucp_analytics — sample HTTPX → BigQuery commerce-observability adapter
for the Universal Commerce Protocol (UCP).

Single-file reference implementation. Four moving parts:

    1. ``classify(...)`` and ``extract_fields(...)`` — pure functions
       that turn an HTTP method/path/body into a UCPEvent.
    2. ``BQWriter`` — an async buffered writer that streams batches of
       rows into a partitioned, clustered BigQuery table.
    3. ``UCPTracker`` — orchestrator. ``.record(response)`` plugs into
       an httpx response event hook (via ``make_event_hook``);
       ``.record_event(event)`` is the manual entry point for events
       that don't pass through HTTPX traffic.
    4. ``SampleAgent`` — reference shape for a UCP shopping agent
       showing where to emit the analytics events the parser can't
       see (payment outcomes, capability negotiation, inbound
       webhook receipts).

Together these cover all 32 event types in the UCP spec: 26 via the
parser, 6 via the agent. Read the file end-to-end, then copy what
you need into your own project and grow it from there.

Anything fancy from a full framework (FastAPI middleware, Google ADK
plugin, MCP/A2A JSON-RPC transports, HTTP message signing parsing,
AP2 mandates, authorization signals, embedded checkout config,
RFC 7235 Bearer challenge parsing, PII redaction) is intentionally
out of scope. Fork the file when you need any of that.

Usage:

    import httpx
    from ucp_analytics import (
        BQWriter, UCPTracker, SampleAgent, make_event_hook,
    )

    writer = BQWriter(
        project_id="my-gcp-project",
        dataset_id="ucp_analytics",
        table_id="ucp_events",
    )
    tracker = UCPTracker(writer)
    agent = SampleAgent(tracker)

    async with httpx.AsyncClient(
        event_hooks={"response": [make_event_hook(tracker)]},
    ) as c:
        # HTTPX traffic — parser captures 26 event types.
        await c.post(
            "https://merchant.example.com/checkout-sessions",
            json={"line_items": [...]},
        )

    # Agent-decision moments — 6 more event types via SampleAgent.
    await agent.payment_completed(
        checkout_session_id="chk_abc", currency="USD", total_amount=3249,
    )

    await writer.close()  # drain the buffer and close the BQ client

Two ways to run the standalone smoke test:

  * ``python ucp_analytics.py`` — stdout-only; no GCP credentials.
    Asserts all 32 UCP event types appear at least once.
  * ``python ucp_analytics.py --e2e --project-id PID [--verify]`` —
    streams the same 33 rows into a real BigQuery table (auto-
    created in dataset ``ucp_analytics_e2e``, table
    ``ucp_events_smoke``). With ``--verify``, polls the table back
    afterwards to confirm rows are queryable. Auth uses Application
    Default Credentials.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import httpx

try:
    from google.cloud import bigquery
except ImportError:  # pragma: no cover — let the demo run without GCP.
    bigquery = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event model + BigQuery schema
# ---------------------------------------------------------------------------

@dataclass
class UCPEvent:
    """One row in the analytics table. Required fields are positional;
    optional fields default to ``None`` so a sparse row is fine."""

    event_type: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    http_method: Optional[str] = None
    http_path: Optional[str] = None
    http_status_code: Optional[int] = None
    merchant_host: Optional[str] = None
    checkout_session_id: Optional[str] = None
    order_id: Optional[str] = None
    currency: Optional[str] = None
    total_amount: Optional[int] = None  # minor units (cents for USD)
    latency_ms: Optional[float] = None
    error_code: Optional[str] = None

    def to_bq_row(self) -> dict:
        """Drop None fields — BigQuery streaming insert prefers absent
        keys over explicit nulls."""
        return {k: v for k, v in asdict(self).items() if v is not None}


# BigQuery schema as (name, type, mode) tuples. Keep it small on
# purpose — add columns here when your dashboards need them.
SCHEMA: List[Tuple[str, str, str]] = [
    ("event_id", "STRING", "REQUIRED"),
    ("event_type", "STRING", "REQUIRED"),
    ("timestamp", "TIMESTAMP", "REQUIRED"),
    ("http_method", "STRING", "NULLABLE"),
    ("http_path", "STRING", "NULLABLE"),
    ("http_status_code", "INTEGER", "NULLABLE"),
    ("merchant_host", "STRING", "NULLABLE"),
    ("checkout_session_id", "STRING", "NULLABLE"),
    ("order_id", "STRING", "NULLABLE"),
    ("currency", "STRING", "NULLABLE"),
    ("total_amount", "INTEGER", "NULLABLE"),
    ("latency_ms", "FLOAT", "NULLABLE"),
    ("error_code", "STRING", "NULLABLE"),
]


# ---------------------------------------------------------------------------
# Parser — pure functions, no I/O
# ---------------------------------------------------------------------------

# Path substrings that mark a request as UCP traffic. The HTTPX hook
# filters on these so non-UCP requests through the same client don't
# get recorded. Webhook prefixes are included so an agent's inbound
# webhook receiver (if it makes HTTPX-visible calls) gets captured;
# real webhook capture typically lives in your server-side middleware
# and would call ``tracker.record_event`` directly.
UCP_PATH_HINTS: Tuple[str, ...] = (
    "/checkout-sessions",
    "/carts",
    "/catalog",
    "/orders",
    "/identity",
    "/oauth2",
    "/.well-known/",
    "/webhooks",
    "/webhook",
)


def is_ucp_path(path: str) -> bool:
    """True if the path matches one of the UCP markers above."""
    return any(hint in path for hint in UCP_PATH_HINTS)


# Well-known fulfillment.events[].type values mapped to ORDER_* lifecycle
# event types per UCP order.md (c5c6139 schema). Anything else is left
# alone; the order falls back to order_get / order_updated / etc.
_FULFILLMENT_TYPE_TO_EVENT = {
    "shipped": "order_shipped",
    "in_transit": "order_shipped",
    "delivered": "order_delivered",
    "returned_to_sender": "order_returned",
    "canceled": "order_canceled",
    "cancelled": "order_canceled",
    "undeliverable": "order_canceled",
}
_ADJUSTMENT_TYPE_TO_EVENT = {
    "refund": "order_returned",
    "return": "order_returned",
    "cancellation": "order_canceled",
}


def _lifecycle_from_body(body: Optional[Any]) -> Optional[str]:
    """Derive an order_* event type from a UCP order response body.

    Priority (matches UCP order.md c5c6139):
      1. Last entry of ``fulfillment.events[]`` — the append-only
         shipment log; latest event wins.
      2. Last entry of ``adjustments[]`` — post-order events
         (refunds, returns, cancellations) when no fulfillment event
         carries lifecycle.
      3. Legacy top-level ``status`` — for pre-c5c6139 senders that
         still ship the flat shape.

    Returns None when no lifecycle signal is present — caller falls
    back to the generic ``order_get`` / ``order_updated`` /
    ``order_webhook_received`` it was about to emit.
    """
    if not isinstance(body, dict):
        return None

    fulfillment = body.get("fulfillment")
    if isinstance(fulfillment, dict):
        events = fulfillment.get("events")
        if isinstance(events, list) and events:
            last = events[-1] if isinstance(events[-1], dict) else None
            if last:
                t = str(last.get("type") or "").lower()
                mapped = _FULFILLMENT_TYPE_TO_EVENT.get(t)
                if mapped:
                    return mapped

    adjustments = body.get("adjustments")
    if isinstance(adjustments, list) and adjustments:
        last = adjustments[-1] if isinstance(adjustments[-1], dict) else None
        if last:
            t = str(last.get("type") or "").lower()
            mapped = _ADJUSTMENT_TYPE_TO_EVENT.get(t)
            if mapped:
                return mapped

    status = str(body.get("status") or "").lower()
    if status == "shipped":
        return "order_shipped"
    if status == "delivered":
        return "order_delivered"
    if status == "returned":
        return "order_returned"
    if status in ("canceled", "cancelled"):
        return "order_canceled"

    return None


def classify(
    method: str, path: str, status_code: int,
    body: Optional[Any] = None,
) -> str:
    """Map HTTP method + path + status (+ optional body) to a UCP event type.

    Intentionally simple: a flat list of substring checks plus a tiny
    body inspector for lifecycle derivation. Real-world deployments
    often need a richer matcher (segment-aware, mount-prefix-aware,
    Standard-Webhooks-header-aware for inbound webhook delivery); copy
    this function out and grow it as needed.

    Coverage: 26 of the 32 UCP spec event types. The other 6 fire at
    agent decision moments (payment outcomes, capability negotiation,
    inbound webhook receipt outside this client's view) and need
    explicit ``tracker.record_event`` calls — see ``SampleAgent``
    below for the shape.
    """
    m = method.upper()
    p = path.rstrip("/")

    # Discovery (REST + OAuth identity-linking metadata).
    if p.endswith("/.well-known/ucp"):
        return "profile_discovered"
    if (
        p.endswith("/.well-known/oauth-authorization-server")
        or p.endswith("/.well-known/openid-configuration")
        or p.endswith("/.well-known/oauth-protected-resource")
    ):
        return "identity_link_initiated"

    if "/checkout-sessions" in p:
        if p.endswith("/complete") and m == "POST":
            return "checkout_session_completed"
        if p.endswith("/cancel") and m == "POST":
            return "checkout_session_canceled"
        if m == "POST":
            return "checkout_session_created"
        if m == "PUT":
            # Escalation override: response body governs.
            if isinstance(body, dict) and body.get("status") == "requires_escalation":
                return "checkout_escalation"
            return "checkout_session_updated"
        if m == "GET":
            return "checkout_session_get"

    if "/carts" in p:
        if p.endswith("/cancel") and m == "POST":
            return "cart_canceled"
        if m == "POST":
            return "cart_created"
        if m == "PUT":
            return "cart_updated"
        if m == "GET":
            return "cart_get"

    if "/catalog/search" in p:
        return "catalog_search"
    if "/catalog/lookup" in p:
        return "catalog_lookup"
    if "/catalog/product" in p:
        return "catalog_product_get"

    # Order webhooks — POST to /webhook(s)/... carrying an order body.
    # Lifecycle wins when the body has it; otherwise generic receipt.
    if "/webhooks" in p or "/webhook" in p:
        if status_code and status_code >= 400:
            return "error"
        lifecycle = _lifecycle_from_body(body)
        return lifecycle or "order_webhook_received"

    if "/orders" in p:
        if m == "POST":
            return "order_created"
        # Lifecycle from body wins on GET/PUT alike.
        lifecycle = _lifecycle_from_body(body)
        if lifecycle:
            return lifecycle
        if m == "PUT":
            return "order_updated"
        return "order_get"

    # Identity linking (/identity/*, /oauth2/*).
    if "/identity" in p or "/oauth2" in p:
        if "/revoke" in p or m == "DELETE":
            return "identity_link_revoked"
        if "/callback" in p or "/oauth2/token" in p:
            return "identity_link_completed"
        return "identity_link_initiated"

    if status_code and status_code >= 400:
        return "error"
    return "request"


def extract_fields(body: Optional[Any]) -> dict:
    """Pull a tiny set of analytics fields out of a UCP JSON body.

    Returns a dict of UCPEvent kwargs (empty if nothing matched). The
    extractor only knows about the few shapes that map cleanly to the
    fixed schema above; everything else is dropped on the floor.

    Heuristic for distinguishing checkouts from orders: order bodies
    carry ``checkout_id`` pointing back at their parent session, while
    checkout bodies do not. So ``{"id": "...", "checkout_id": "..."}``
    is an order; ``{"id": "..."}`` is a checkout.
    """
    if not isinstance(body, dict):
        return {}
    out: dict = {}

    raw_id = body.get("id")
    if isinstance(raw_id, str) and raw_id:
        if "checkout_id" in body:
            out["order_id"] = raw_id
            out["checkout_session_id"] = body["checkout_id"]
        else:
            out["checkout_session_id"] = raw_id

    currency = body.get("currency")
    if isinstance(currency, str) and currency:
        out["currency"] = currency

    # `totals` is an array of {type, amount, ...}. Sum every entry of
    # type=="total" (multiple detail rows per type are spec-permitted).
    totals = body.get("totals")
    if isinstance(totals, list):
        running = 0
        seen_total = False
        for entry in totals:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "total" and isinstance(entry.get("amount"), int):
                # bool is a subclass of int; reject so True doesn't
                # smuggle a 1 into a SUM.
                amt = entry["amount"]
                if isinstance(amt, bool):
                    continue
                running += amt
                seen_total = True
        if seen_total:
            out["total_amount"] = running

    # First error code from messages[] (the array carries multiple
    # severities; we only mirror the first error onto a scalar).
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if (
                isinstance(msg, dict)
                and msg.get("type") == "error"
                and isinstance(msg.get("code"), str)
            ):
                out["error_code"] = msg["code"]
                break

    return out


# ---------------------------------------------------------------------------
# BigQuery writer — async, buffered, with auto-create
# ---------------------------------------------------------------------------

class BQWriter:
    """Buffers UCPEvent rows in memory and streams them into a
    partitioned, clustered BigQuery table.

    Synchronous BigQuery client calls are dispatched via
    ``asyncio.to_thread`` so the event loop stays unblocked when the
    writer is used inside an async web server or HTTPX client.

    Constructor knobs:
      * ``batch_size`` — flush whenever the buffer hits this size.
        Default 50. ``flush()`` and ``close()`` also drain the buffer.
      * ``auto_create`` — create the dataset and table on first write
        if missing. Off by default; turn on for greenfield setups.
      * ``max_buffer_size`` — hard cap to keep memory bounded if BQ is
        persistently unreachable. Oldest events get dropped first.
    """

    def __init__(
        self,
        project_id: str,
        dataset_id: str,
        table_id: str = "ucp_events",
        *,
        batch_size: int = 50,
        auto_create: bool = True,
        max_buffer_size: int = 10_000,
    ) -> None:
        if bigquery is None:
            raise RuntimeError(
                "google-cloud-bigquery is required for BQWriter; "
                "`pip install google-cloud-bigquery`"
            )
        self.project_id = project_id
        self.dataset_id = dataset_id
        self.table_id = table_id
        self.batch_size = batch_size
        self.auto_create = auto_create
        self.max_buffer_size = max_buffer_size
        self._buffer: List[dict] = []
        self._lock = asyncio.Lock()
        self._client: Optional["bigquery.Client"] = None
        self._table_ready = False

    @property
    def full_table_id(self) -> str:
        return f"{self.project_id}.{self.dataset_id}.{self.table_id}"

    # ---- public API ----

    async def enqueue(self, row: dict) -> None:
        """Add a row to the buffer; flush if we've hit ``batch_size``."""
        async with self._lock:
            self._buffer.append(row)
            # Hard cap: drop oldest if we'd grow past max_buffer_size.
            # This is an explicit choice for "lossy but bounded" over
            # OOM on a long BigQuery outage.
            overflow = len(self._buffer) - self.max_buffer_size
            if overflow > 0:
                dropped = self._buffer[:overflow]
                self._buffer = self._buffer[overflow:]
                logger.warning(
                    "BQWriter buffer full; dropped %d oldest rows", len(dropped)
                )
            if len(self._buffer) >= self.batch_size:
                await self._flush_locked()

    async def flush(self) -> None:
        """Force-flush whatever's in the buffer."""
        async with self._lock:
            await self._flush_locked()

    async def close(self) -> None:
        """Flush + release the BigQuery client."""
        await self.flush()
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- internals ----

    def _init_client_sync(self) -> None:
        if self._client is None:
            self._client = bigquery.Client(project=self.project_id)

    def _ensure_table_sync(self) -> None:
        if self._table_ready:
            return
        self._init_client_sync()
        assert self._client is not None
        if self.auto_create:
            try:
                self._client.create_dataset(self.dataset_id, exists_ok=True)
            except Exception as exc:
                logger.warning("create_dataset failed: %s", exc)
            ref = self._client.dataset(self.dataset_id).table(self.table_id)
            schema = [
                bigquery.SchemaField(name, dtype, mode=mode)
                for name, dtype, mode in SCHEMA
            ]
            table = bigquery.Table(ref, schema=schema)
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="timestamp",
            )
            table.clustering_fields = ["event_type", "checkout_session_id"]
            try:
                self._client.create_table(table, exists_ok=True)
            except Exception as exc:
                logger.warning("create_table failed: %s", exc)
        self._table_ready = True

    def _insert_sync(self, rows: List[dict]) -> List[dict]:
        """Stream-insert ``rows``, returning any that BigQuery rejected
        at the row level. Network/auth errors bubble up to the caller."""
        self._ensure_table_sync()
        assert self._client is not None
        row_errors = self._client.insert_rows_json(self.full_table_id, rows)
        if not row_errors:
            return []
        failed_idx = {e["index"] for e in row_errors if "index" in e}
        logger.warning(
            "BigQuery rejected %d of %d rows on stream insert",
            len(failed_idx), len(rows),
        )
        return [rows[i] for i in sorted(failed_idx) if 0 <= i < len(rows)]

    async def _flush_locked(self) -> None:
        if not self._buffer:
            return
        batch = self._buffer
        self._buffer = []
        try:
            failed = await asyncio.to_thread(self._insert_sync, batch)
        except Exception as exc:
            logger.exception("flush raised; requeueing whole batch: %s", exc)
            # Re-queue at the front, then re-cap to max_buffer_size.
            self._buffer = (batch + self._buffer)[-self.max_buffer_size:]
            return
        if failed:
            self._buffer = (failed + self._buffer)[-self.max_buffer_size:]


# ---------------------------------------------------------------------------
# HTTPX glue
# ---------------------------------------------------------------------------

class UCPTracker:
    """Connects the parser to a writer.

    Two entry points:

      * ``record(response)`` — drive from HTTPX response traffic.
        Parses method/path/body, classifies, extracts fields, enqueues.
      * ``record_event(event)`` — for events that don't pass through
        HTTPX (agent decision moments, server-side webhook receipts,
        out-of-band lifecycle pings). ``SampleAgent`` below shows the
        common cases.
    """

    def __init__(self, writer: BQWriter) -> None:
        self.writer = writer

    async def record(self, response: httpx.Response) -> None:
        request = response.request
        path = request.url.path
        if not is_ucp_path(path):
            return

        # Make sure the body is available; httpx defers reads on
        # streaming responses, and our parser needs the JSON.
        try:
            await response.aread()
        except Exception:
            pass

        body: Optional[Any] = None
        try:
            body = response.json()
        except Exception:
            body = None

        latency_ms: Optional[float] = None
        elapsed = response.elapsed
        if elapsed is not None:
            latency_ms = round(elapsed.total_seconds() * 1000, 2)

        event = UCPEvent(
            event_type=classify(
                request.method, path, response.status_code, body,
            ),
            http_method=request.method.upper(),
            http_path=path,
            http_status_code=response.status_code,
            merchant_host=request.url.host or None,
            latency_ms=latency_ms,
            **extract_fields(body),
        )
        await self.writer.enqueue(event.to_bq_row())

    async def record_event(self, event: UCPEvent) -> None:
        """Enqueue a pre-built event. For things HTTPX can't see:
        agent payment decisions, capability negotiation outcomes,
        inbound webhook receipts processed by your server-side
        handler, lifecycle pings from out-of-band sources.
        """
        await self.writer.enqueue(event.to_bq_row())


# ---------------------------------------------------------------------------
# SampleAgent — where the parser stops and your agent code starts
# ---------------------------------------------------------------------------

class SampleAgent:
    """Reference shape for a UCP shopping agent's analytics emission.

    HTTPX hooks capture wire traffic; they can't see decisions the
    agent makes between requests. These six methods are the spots in
    a real agent loop where you'd call ``tracker.record_event`` to
    cover the rest of the spec:

      * ``capability_negotiated`` — after parsing ``/.well-known/ucp``
        and picking which UCP feature set to use for this merchant.
      * ``payment_handler_negotiated`` — after the agent selects a
        payment handler (AP2 / network token / wallet) for the
        session.
      * ``payment_instrument_selected`` — when the user picks a card
        / wallet / saved instrument.
      * ``payment_completed`` / ``payment_failed`` — terminal payment
        outcomes the merchant returned (often piggybacked on
        checkout_session_completed, but worth emitting separately so
        payment dashboards don't have to dig into checkout bodies).
      * ``webhook_received`` — inbound webhook your server-side
        handler accepted (the HTTPX-side parser only sees outbound
        traffic; webhook delivery is inbound).

    Copy or subclass; nothing here is load-bearing beyond the
    ``record_event`` call.
    """

    def __init__(self, tracker: UCPTracker) -> None:
        self.tracker = tracker

    async def capability_negotiated(
        self,
        *,
        merchant_host: Optional[str] = None,
        checkout_session_id: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="capability_negotiated",
            merchant_host=merchant_host,
            checkout_session_id=checkout_session_id,
        ))

    async def payment_handler_negotiated(
        self,
        *,
        checkout_session_id: Optional[str] = None,
        merchant_host: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="payment_handler_negotiated",
            checkout_session_id=checkout_session_id,
            merchant_host=merchant_host,
        ))

    async def payment_instrument_selected(
        self,
        *,
        checkout_session_id: Optional[str] = None,
        merchant_host: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="payment_instrument_selected",
            checkout_session_id=checkout_session_id,
            merchant_host=merchant_host,
        ))

    async def payment_completed(
        self,
        *,
        checkout_session_id: Optional[str] = None,
        order_id: Optional[str] = None,
        currency: Optional[str] = None,
        total_amount: Optional[int] = None,
        merchant_host: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="payment_completed",
            checkout_session_id=checkout_session_id,
            order_id=order_id,
            currency=currency,
            total_amount=total_amount,
            merchant_host=merchant_host,
        ))

    async def payment_failed(
        self,
        *,
        checkout_session_id: Optional[str] = None,
        merchant_host: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="payment_failed",
            checkout_session_id=checkout_session_id,
            merchant_host=merchant_host,
            error_code=error_code,
        ))

    async def webhook_received(
        self,
        *,
        order_id: Optional[str] = None,
        merchant_host: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="order_webhook_received",
            order_id=order_id,
            merchant_host=merchant_host,
        ))


def make_event_hook(tracker: UCPTracker):
    """Return an httpx response event hook bound to ``tracker``.

    Wraps ``tracker.record`` in a broad exception handler so a
    misbehaving response (e.g. unexpected body shape) can't take down
    the calling client.
    """
    async def _hook(response: httpx.Response) -> None:
        try:
            await tracker.record(response)
        except Exception:
            logger.exception("UCP analytics record failed; dropping event")
    return _hook


# ---------------------------------------------------------------------------
# Standalone smoke test — `python ucp_analytics.py [--e2e ...]`
# ---------------------------------------------------------------------------
#
# Two modes, both driven by the same event generator:
#
#   * Default (no args): writes rows to stdout via ``_PrintWriter``.
#     No GCP credentials needed; asserts all 32 UCP spec events appear.
#   * ``--e2e --project-id PID``: streams rows through ``BQWriter`` to
#     a real BigQuery table (auto-created). With ``--verify``, polls
#     the table back to confirm rows are queryable.

class _PrintWriter:
    """Drop-in stand-in for BQWriter that prints rows as JSON lines."""

    async def enqueue(self, row: dict) -> None:
        import json
        print(json.dumps(row))

    async def flush(self) -> None:  # pragma: no cover
        pass

    async def close(self) -> None:  # pragma: no cover
        pass


class _CoverageTeeWriter:
    """Wraps any writer to record distinct event_types seen, so the
    coverage check is independent of whether the underlying writer
    keeps rows in memory (PrintWriter does not; BQWriter streams)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.seen_types: set = set()
        self.row_count = 0

    async def enqueue(self, row: dict) -> None:
        et = row.get("event_type")
        if et:
            self.seen_types.add(et)
        self.row_count += 1
        await self.inner.enqueue(row)

    async def flush(self) -> None:
        await self.inner.flush()

    async def close(self) -> None:
        await self.inner.close()


# All 32 UCP spec event types — the smoke test below exercises every
# one and asserts the coverage at exit.
ALL_UCP_EVENT_TYPES: Tuple[str, ...] = (
    # Discovery (1)
    "profile_discovered",
    # Checkout (6)
    "checkout_session_created",
    "checkout_session_get",
    "checkout_session_updated",
    "checkout_session_completed",
    "checkout_session_canceled",
    "checkout_escalation",
    # Cart (4)
    "cart_created",
    "cart_get",
    "cart_updated",
    "cart_canceled",
    # Catalog (3)
    "catalog_search",
    "catalog_lookup",
    "catalog_product_get",
    # Order (8)
    "order_created",
    "order_get",
    "order_updated",
    "order_shipped",
    "order_delivered",
    "order_returned",
    "order_canceled",
    "order_webhook_received",
    # Identity linking (3)
    "identity_link_initiated",
    "identity_link_completed",
    "identity_link_revoked",
    # Fallback (2)
    "error",
    "request",
    # Agent-emitted — not visible to HTTPX (5)
    "capability_negotiated",
    "payment_handler_negotiated",
    "payment_instrument_selected",
    "payment_completed",
    "payment_failed",
)


async def _smoke_test(
    writer: Any, *, merchant_tag: str = "shop.example.com",
) -> None:
    """Drive 33 fake responses + agent calls through ``writer``.

    Parametric on writer so the same generator backs both the stdout
    smoke (``_PrintWriter``) and the BigQuery e2e (``BQWriter``).

    ``merchant_tag`` is used as the HTTPX host AND as the
    ``merchant_host`` argument to every agent call, so e2e mode can
    filter the table back down to the rows from this specific run.
    """
    from datetime import timedelta

    tracker = UCPTracker(writer)  # type: ignore[arg-type]
    agent = SampleAgent(tracker)

    host = f"https://{merchant_tag}"

    # 26 HTTPX-visible samples (one per parser-derivable event type).
    samples = [
        ("GET", f"{host}/.well-known/ucp", 200, None),
        # Checkout (6)
        (
            "POST", f"{host}/checkout-sessions", 201,
            {"id": "chk_abc", "currency": "USD",
             "totals": [{"type": "subtotal", "amount": 2999}]},
        ),
        ("GET", f"{host}/checkout-sessions/chk_abc", 200, {"id": "chk_abc"}),
        (
            "PUT", f"{host}/checkout-sessions/chk_abc", 200,
            {"id": "chk_abc", "status": "active"},
        ),
        (
            "POST", f"{host}/checkout-sessions/chk_abc/complete", 200,
            {"id": "chk_abc", "currency": "USD",
             "totals": [
                 {"type": "subtotal", "amount": 2999},
                 {"type": "tax", "amount": 250},
                 {"type": "total", "amount": 3249},
             ]},
        ),
        (
            "POST", f"{host}/checkout-sessions/chk_abc/cancel", 200,
            {"id": "chk_abc"},
        ),
        (
            "PUT", f"{host}/checkout-sessions/chk_esc", 200,
            {"id": "chk_esc", "status": "requires_escalation"},
        ),
        # Cart (4)
        ("POST", f"{host}/carts", 201, {"id": "cart_1"}),
        ("GET", f"{host}/carts/cart_1", 200, {"id": "cart_1"}),
        ("PUT", f"{host}/carts/cart_1", 200, {"id": "cart_1"}),
        ("POST", f"{host}/carts/cart_1/cancel", 200, {"id": "cart_1"}),
        # Catalog (3)
        ("GET", f"{host}/catalog/search?q=roses", 200, None),
        ("POST", f"{host}/catalog/lookup", 200, None),
        ("GET", f"{host}/catalog/product/sku_1", 200, None),
        # Order (8) — webhook + 4 lifecycle from fulfillment.events[]
        (
            "POST", f"{host}/orders", 201,
            {"id": "order_xyz", "checkout_id": "chk_abc", "currency": "USD"},
        ),
        ("GET", f"{host}/orders/order_xyz", 200, {"id": "order_xyz"}),
        ("PUT", f"{host}/orders/order_xyz", 200, {"id": "order_xyz"}),
        (
            "GET", f"{host}/orders/order_xyz", 200,
            {"id": "order_xyz",
             "fulfillment": {"events": [{"type": "shipped"}]}},
        ),
        (
            "GET", f"{host}/orders/order_xyz", 200,
            {"id": "order_xyz",
             "fulfillment": {"events": [
                 {"type": "shipped"}, {"type": "delivered"},
             ]}},
        ),
        (
            "GET", f"{host}/orders/order_xyz", 200,
            {"id": "order_xyz",
             "adjustments": [{"type": "refund"}]},
        ),
        (
            "GET", f"{host}/orders/order_xyz", 200,
            {"id": "order_xyz", "status": "canceled"},
        ),
        (
            "POST", f"{host}/webhooks/orders", 200,
            {"id": "order_xyz"},  # generic receipt — no lifecycle in body
        ),
        # Identity linking (3) — initiated via discovery, completed via
        # callback, revoked via DELETE.
        ("GET", f"{host}/.well-known/oauth-authorization-server", 200, None),
        ("POST", f"{host}/identity/callback", 200, None),
        ("DELETE", f"{host}/identity/link/abc", 204, None),
        # Fallback (2). `error` and `request` only fire when the path
        # passes ``is_ucp_path`` but no specific branch matches.
        # Most UCP branches catch their paths regardless of method, so
        # we use unhandled (method, path) combinations to fall through:
        #   - PATCH /carts/cart_1 — carts branch only handles
        #     POST/PUT/GET/cancel; PATCH falls past it.
        #   - OPTIONS /catalog/info — catalog branch only handles
        #     /catalog/{search,lookup,product}; /catalog/info matches
        #     the UCP hint but no specific branch.
        ("PATCH", f"{host}/carts/cart_1", 500, None),  # → error
        ("OPTIONS", f"{host}/catalog/info", 200, None),  # → request
    ]
    for method, url, status, body in samples:
        request = httpx.Request(method, url)
        response = httpx.Response(status_code=status, request=request, json=body)
        response._elapsed = timedelta(milliseconds=42)
        await tracker.record(response)

    # 5 agent-decision events (the remaining types). Note: the spec
    # has 32 types total; the parser owns 26, the agent owns 6, but
    # one of those — ``order_webhook_received`` — is already firable
    # from inbound HTTPX traffic above. SampleAgent.webhook_received
    # is the entry point for server-side handlers; we exercise it
    # here for parity but it overlaps with the webhook sample row.
    await agent.capability_negotiated(merchant_host=merchant_tag)
    await agent.payment_handler_negotiated(
        checkout_session_id="chk_abc", merchant_host=merchant_tag,
    )
    await agent.payment_instrument_selected(
        checkout_session_id="chk_abc", merchant_host=merchant_tag,
    )
    await agent.payment_completed(
        checkout_session_id="chk_abc",
        order_id="order_xyz",
        currency="USD",
        total_amount=3249,
        merchant_host=merchant_tag,
    )
    await agent.payment_failed(
        checkout_session_id="chk_fail",
        error_code="card_declined",
        merchant_host=merchant_tag,
    )
    await agent.webhook_received(
        order_id="order_xyz", merchant_host=merchant_tag,
    )


def _check_coverage(seen: set, row_count: int) -> None:
    """Compare ``seen`` against ``ALL_UCP_EVENT_TYPES``. Exits non-zero
    if anything's missing; warns on extras (forward-compatible)."""
    expected = set(ALL_UCP_EVENT_TYPES)
    missing = expected - seen
    extra = seen - expected
    print(
        f"\n# {row_count} rows recorded; "
        f"{len(seen)}/{len(expected)} distinct event types",
        flush=True,
    )
    if missing:
        raise SystemExit(f"missing event types: {sorted(missing)}")
    if extra:
        print(
            f"# note: extra event types not in spec list: {sorted(extra)}",
            flush=True,
        )


async def _run_print() -> None:
    """Default mode: print rows to stdout, no GCP creds."""
    tee = _CoverageTeeWriter(_PrintWriter())
    await _smoke_test(tee)
    _check_coverage(tee.seen_types, tee.row_count)


async def _run_e2e(
    *,
    project_id: str,
    dataset_id: str,
    table_id: str,
    verify: bool,
    verify_timeout_s: float,
) -> None:
    """E2E mode: stream rows through ``BQWriter`` to a real BigQuery
    table. With ``verify=True``, polls the table back afterwards.

    Auth uses Application Default Credentials. Configure with
    ``gcloud auth application-default login`` or
    ``GOOGLE_APPLICATION_CREDENTIALS``.
    """
    if bigquery is None:
        raise SystemExit(
            "google-cloud-bigquery is not installed; e2e mode requires it "
            "(`pip install google-cloud-bigquery`)"
        )

    run_id = uuid.uuid4().hex[:12]
    merchant_tag = f"smoke-{run_id}.example.com"
    run_start = datetime.now(timezone.utc).isoformat()

    writer = BQWriter(
        project_id=project_id,
        dataset_id=dataset_id,
        table_id=table_id,
        batch_size=10,
    )
    tee = _CoverageTeeWriter(writer)

    print(f"# e2e mode: streaming to {writer.full_table_id}", flush=True)
    print(f"# run tag: merchant_host = {merchant_tag!r}", flush=True)

    try:
        await _smoke_test(tee, merchant_tag=merchant_tag)
    finally:
        await writer.close()  # drain + flush whatever's buffered

    _check_coverage(tee.seen_types, tee.row_count)

    if verify:
        await _verify_e2e(
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            merchant_tag=merchant_tag,
            run_start_iso=run_start,
            expected=tee.row_count,
            timeout_s=verify_timeout_s,
        )


async def _verify_e2e(
    *,
    project_id: str,
    dataset_id: str,
    table_id: str,
    merchant_tag: str,
    run_start_iso: str,
    expected: int,
    timeout_s: float,
) -> None:
    """Poll BigQuery until every row from this run is queryable.

    Streaming-buffer rows are usually visible within a few seconds but
    can take longer; we poll with backoff for up to ``timeout_s`` and
    fail if we don't hit ``expected``.
    """
    client = bigquery.Client(project=project_id)
    table_ref = f"{project_id}.{dataset_id}.{table_id}"
    sql = f"""
        SELECT COUNT(*) AS n
        FROM `{table_ref}`
        WHERE merchant_host = @tag
          AND timestamp >= TIMESTAMP(@since)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("tag", "STRING", merchant_tag),
            bigquery.ScalarQueryParameter("since", "STRING", run_start_iso),
        ]
    )

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    delay = 2.0
    n = 0
    while loop.time() < deadline:
        job = await asyncio.to_thread(client.query, sql, job_config=job_config)
        rows = await asyncio.to_thread(lambda: list(job.result()))
        n = int(rows[0]["n"]) if rows else 0
        if n >= expected:
            break
        remaining = max(0.0, deadline - loop.time())
        print(
            f"# verify: {n}/{expected} rows visible; "
            f"sleeping {delay:.1f}s (≤{remaining:.0f}s left)",
            flush=True,
        )
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 10.0)

    try:
        client.close()
    except Exception:
        pass

    if n < expected:
        raise SystemExit(
            f"verify failed: {n}/{expected} rows visible in {table_ref} "
            f"after {timeout_s:.0f}s (streaming buffer may still be "
            "draining; re-query manually if needed)"
        )
    print(
        f"# verify ok: {n} rows visible in {table_ref} "
        f"with merchant_host={merchant_tag!r}",
        flush=True,
    )


def _main_cli() -> None:
    """CLI entry point. ``python ucp_analytics.py`` for stdout smoke;
    ``python ucp_analytics.py --e2e --project-id PID`` for BigQuery."""
    import argparse
    p = argparse.ArgumentParser(
        prog="ucp_analytics",
        description=(
            "Sample UCP analytics adapter. With no args, runs an offline "
            "smoke test that prints rows to stdout and asserts all 32 UCP "
            "spec events are emitted. With --e2e, streams the same rows "
            "to a real BigQuery table for end-to-end verification."
        ),
    )
    p.add_argument(
        "--e2e", action="store_true",
        help="Run end-to-end against real BigQuery instead of stdout.",
    )
    p.add_argument(
        "--project-id",
        help="GCP project ID for BigQuery (required with --e2e).",
    )
    p.add_argument(
        "--dataset-id", default="ucp_analytics_e2e",
        help="BigQuery dataset, auto-created. Default: ucp_analytics_e2e",
    )
    p.add_argument(
        "--table-id", default="ucp_events_smoke",
        help="BigQuery table, auto-created. Default: ucp_events_smoke",
    )
    p.add_argument(
        "--verify", action="store_true",
        help="After flushing, poll BigQuery to confirm every row from "
             "this run is queryable. Streaming buffer can lag a few "
             "seconds; we poll with backoff up to --verify-timeout.",
    )
    p.add_argument(
        "--verify-timeout", type=float, default=90.0,
        help="Seconds to poll the table for during --verify. Default: 90",
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.e2e:
        if not args.project_id:
            raise SystemExit("--e2e requires --project-id")
        asyncio.run(_run_e2e(
            project_id=args.project_id,
            dataset_id=args.dataset_id,
            table_id=args.table_id,
            verify=args.verify,
            verify_timeout_s=args.verify_timeout,
        ))
    else:
        asyncio.run(_run_print())


if __name__ == "__main__":  # pragma: no cover
    _main_cli()
