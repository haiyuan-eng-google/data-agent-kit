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

Sample reference implementation. Three moving parts in this file:

    1. ``classify(...)`` and ``extract_fields(...)`` — pure functions
       that turn an HTTP method/path/body into a UCPEvent.
    2. ``BQWriter`` — an async buffered writer that streams batches of
       rows into a partitioned, clustered BigQuery table.
    3. ``UCPTracker`` — orchestrator. ``.record(response)`` plugs into
       an httpx response event hook (via ``make_event_hook``);
       ``.record_event(event)`` is the manual entry point for events
       that don't pass through HTTPX traffic.

A sibling file ``sample_agent.py`` provides ``SampleAgent``, a
reference shape for a UCP shopping agent that emits the events the
parser cannot see (payment outcomes, capability negotiation,
server-side webhook receipts).

Together they cover all 32 event types in the UCP spec:

  * 27 derivable from HTTPX traffic (the parser).
  * 6 emitted from the agent loop (``SampleAgent``).
  * 1 overlap (``order_webhook_received``) reachable from either
    surface — the parser sees it on inbound HTTPX POSTs to
    ``/webhook(s)/...``; the agent's ``webhook_received`` method is
    the entry point when your server-side handler is the one
    receiving the webhook.

So: 27 + 6 - 1 = 32. Read both files end-to-end, then copy what
you need into your own project and grow it from there.

Anything fancy from a full framework (FastAPI middleware, Google ADK
plugin, MCP/A2A JSON-RPC transports, HTTP message signing parsing,
AP2 mandates, authorization signals, embedded checkout config,
RFC 7235 Bearer challenge parsing, PII redaction) is intentionally
out of scope. Fork the file when you need any of that.

A runnable quickstart lives in the sibling file ``quickstart.py``;
the regression smoke test lives in ``smoke_test.py``.
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

    Coverage: 27 of the 32 UCP spec event types are derivable from
    HTTPX traffic (the parser). The other 5 fire only at agent
    decision moments (payment outcomes, capability negotiation,
    payment handler / instrument selection) and need explicit
    ``tracker.record_event`` calls — see ``SampleAgent`` in
    ``sample_agent.py`` for the shape. ``order_webhook_received``
    is reachable from both
    surfaces (parser sees outbound POSTs to ``/webhook(s)``;
    ``SampleAgent.webhook_received`` is the entry point for
    server-side handlers).
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


def extract_fields(
    body: Optional[Any], event_type: Optional[str] = None,
) -> dict:
    """Pull a tiny set of analytics fields out of a UCP JSON body.

    Returns a dict of UCPEvent kwargs (empty if nothing matched). The
    extractor only knows about the few shapes that map cleanly to the
    fixed schema above; everything else is dropped on the floor.

    The ``event_type`` arg (set by ``UCPTracker.record``) routes
    ``body["id"]`` to the right column:

      * checkout_* events → ``checkout_session_id``
      * order_*    events → ``order_id`` (plus ``checkout_session_id``
        if ``checkout_id`` is present, since orders carry a backref
        to their parent session)
      * cart_* / catalog_* / identity_* / discovery / fallback → the
        id is intentionally dropped; the schema has no cart_id /
        product_id column and conflating them with checkout_session_id
        corrupts session-level aggregation.

    If ``event_type`` is omitted (legacy callers), falls back to the
    older "checkout_id present → order, else checkout" heuristic.
    """
    if not isinstance(body, dict):
        return {}
    out: dict = {}

    raw_id = body.get("id")
    if isinstance(raw_id, str) and raw_id:
        et = event_type or ""
        if et.startswith("order_") or "checkout_id" in body:
            out["order_id"] = raw_id
            backref = body.get("checkout_id")
            if isinstance(backref, str) and backref:
                out["checkout_session_id"] = backref
        elif et.startswith("checkout_") or not et:
            out["checkout_session_id"] = raw_id
        # cart_*, catalog_*, identity_*, profile_discovered, error,
        # request → no column for this id; dropping is intentional.

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
        # Exposed so callers (e.g. e2e mode) can detect that the final
        # flush didn't drain the buffer or that an insert raised.
        # ``last_flush_error`` is cleared on every successful flush; set
        # to the most recent exception otherwise.
        self.last_flush_error: Optional[BaseException] = None

    @property
    def full_table_id(self) -> str:
        return f"{self.project_id}.{self.dataset_id}.{self.table_id}"

    @property
    def buffered_count(self) -> int:
        """How many rows are still waiting in the in-memory buffer.
        After ``close()``, a non-zero value means the final flush could
        not drain — typically a persistent BigQuery error."""
        return len(self._buffer)

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
            self.last_flush_error = None
            return
        batch = self._buffer
        self._buffer = []
        try:
            failed = await asyncio.to_thread(self._insert_sync, batch)
        except Exception as exc:
            logger.exception("flush raised; requeueing whole batch: %s", exc)
            # Re-queue at the front, then re-cap to max_buffer_size.
            self._buffer = (batch + self._buffer)[-self.max_buffer_size:]
            self.last_flush_error = exc
            return
        if failed:
            self._buffer = (failed + self._buffer)[-self.max_buffer_size:]
            # Partial row-level rejections aren't a flush failure per se
            # — the call succeeded; just some rows were bad. Leave
            # ``last_flush_error`` cleared so callers don't conflate.
            self.last_flush_error = None
        else:
            self.last_flush_error = None


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
        out-of-band lifecycle pings). ``SampleAgent`` in
        ``sample_agent.py`` shows the common cases.
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
        # ``response.elapsed`` is populated by httpx once the response
        # is closed. Inside a response event hook the response is
        # already readable, but ``elapsed`` raises ``RuntimeError`` on
        # transports that don't track wall time (e.g. ``MockTransport``)
        # and on some error paths. Treat the field as best-effort.
        try:
            elapsed = response.elapsed
        except RuntimeError:
            elapsed = None
        if elapsed is not None:
            latency_ms = round(elapsed.total_seconds() * 1000, 2)

        event_type = classify(
            request.method, path, response.status_code, body,
        )
        event = UCPEvent(
            event_type=event_type,
            http_method=request.method.upper(),
            http_path=path,
            http_status_code=response.status_code,
            merchant_host=request.url.host or None,
            latency_ms=latency_ms,
            **extract_fields(body, event_type),
        )
        await self.writer.enqueue(event.to_bq_row())

    async def record_event(self, event: UCPEvent) -> None:
        """Enqueue a pre-built event. For things HTTPX can't see:
        agent payment decisions, capability negotiation outcomes,
        inbound webhook receipts processed by your server-side
        handler, lifecycle pings from out-of-band sources.
        """
        await self.writer.enqueue(event.to_bq_row())


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
