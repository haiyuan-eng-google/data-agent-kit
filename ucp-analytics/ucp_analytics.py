"""ucp_analytics — sample HTTPX → BigQuery commerce-observability adapter
for the Universal Commerce Protocol (UCP).

This is a single-file reference implementation. It exposes three small
moving parts:

    1. ``classify(...)`` and ``extract_fields(...)`` — pure functions
       that turn an HTTP method/path/body into a UCPEvent.
    2. ``BQWriter`` — an async buffered writer that streams batches of
       rows into a partitioned, clustered BigQuery table.
    3. ``UCPTracker.record(response)`` plus ``make_event_hook(tracker)``
       — glue that lets you attach the tracker as an httpx response
       event hook.

The goal is to be small enough to read in one sitting and copy into
your own project. Fork the file and add the columns, classification
rules, batching strategy, redaction, and retry policy your workload
needs. Anything fancy (FastAPI middleware, ADK plugin, MCP/A2A
transports, HTTP message signing, AP2 mandates, signals, OAuth
discovery, embedded checkout, Standard Webhooks header parsing,
RFC 7235 Bearer challenge parsing, fulfillment-events lifecycle
derivation, PII redaction) is intentionally out of scope.

Usage:

    import httpx
    from ucp_analytics import BQWriter, UCPTracker, make_event_hook

    writer = BQWriter(
        project_id="my-gcp-project",
        dataset_id="ucp_analytics",
        table_id="ucp_events",
    )
    tracker = UCPTracker(writer)
    hook = make_event_hook(tracker)

    async with httpx.AsyncClient(event_hooks={"response": [hook]}) as c:
        await c.post(
            "https://merchant.example.com/checkout-sessions",
            json={"line_items": [...]},
        )

    await writer.close()  # drains the buffer and closes the BQ client

Run ``python ucp_analytics.py`` with no arguments for a stdout-only
smoke test of the parser + writer (no GCP credentials required).
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
# get recorded.
UCP_PATH_HINTS: Tuple[str, ...] = (
    "/checkout-sessions",
    "/carts",
    "/catalog",
    "/orders",
    "/identity",
    "/.well-known/ucp",
)


def is_ucp_path(path: str) -> bool:
    """True if the path matches one of the UCP markers above."""
    return any(hint in path for hint in UCP_PATH_HINTS)


def classify(method: str, path: str, status_code: int) -> str:
    """Map HTTP method + path + status to a UCP event type.

    Intentionally simple: a flat list of substring checks. Real-world
    deployments often need a richer matcher (segment-aware,
    mount-prefix-aware, header-aware for webhook delivery); copy this
    function out and grow it as needed.
    """
    m = method.upper()
    p = path.rstrip("/")

    if p.endswith("/.well-known/ucp"):
        return "profile_discovered"

    if "/checkout-sessions" in p:
        if p.endswith("/complete") and m == "POST":
            return "checkout_session_completed"
        if p.endswith("/cancel") and m == "POST":
            return "checkout_session_canceled"
        if m == "POST":
            return "checkout_session_created"
        if m == "PUT":
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

    if "/orders" in p:
        if m == "POST":
            return "order_created"
        if m == "PUT":
            return "order_updated"
        if m == "GET":
            return "order_get"

    if "/identity" in p:
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
    """Connects the parser to a writer. ``record(response)`` is the
    single entry point — call it from an httpx response event hook,
    a FastAPI middleware, or your own custom integration."""

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
            event_type=classify(request.method, path, response.status_code),
            http_method=request.method.upper(),
            http_path=path,
            http_status_code=response.status_code,
            merchant_host=request.url.host or None,
            latency_ms=latency_ms,
            **extract_fields(body),
        )
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


# ---------------------------------------------------------------------------
# Standalone smoke test — `python ucp_analytics.py`
# ---------------------------------------------------------------------------
#
# Stub writer that prints rows to stdout. Lets you sanity-check the
# parser + classifier without GCP credentials.

class _PrintWriter:
    """Drop-in stand-in for BQWriter that prints rows as JSON lines."""

    def __init__(self) -> None:
        self.rows: List[dict] = []

    async def enqueue(self, row: dict) -> None:
        import json
        print(json.dumps(row))
        self.rows.append(row)

    async def flush(self) -> None:  # pragma: no cover
        pass

    async def close(self) -> None:  # pragma: no cover
        pass


async def _smoke_test() -> None:
    """Run a handful of fake responses through the parser."""
    writer = _PrintWriter()
    tracker = UCPTracker(writer)  # type: ignore[arg-type]

    samples = [
        ("GET", "https://shop.example.com/.well-known/ucp", 200, None),
        (
            "POST", "https://shop.example.com/checkout-sessions", 201,
            {
                "id": "chk_abc",
                "currency": "USD",
                "totals": [{"type": "subtotal", "amount": 2999}],
            },
        ),
        (
            "POST", "https://shop.example.com/checkout-sessions/chk_abc/complete",
            200,
            {
                "id": "chk_abc",
                "currency": "USD",
                "totals": [
                    {"type": "subtotal", "amount": 2999},
                    {"type": "tax", "amount": 250},
                    {"type": "total", "amount": 3249},
                ],
            },
        ),
        (
            "POST", "https://shop.example.com/orders", 201,
            {"id": "order_xyz", "checkout_id": "chk_abc", "currency": "USD"},
        ),
        (
            "GET", "https://shop.example.com/orders/order_xyz", 404,
            {"messages": [{"type": "error", "code": "not_found"}]},
        ),
    ]
    for method, url, status, body in samples:
        request = httpx.Request(method, url)
        response = httpx.Response(status_code=status, request=request, json=body)
        from datetime import timedelta
        response._elapsed = timedelta(milliseconds=42)
        await tracker.record(response)

    print(f"\n# {len(writer.rows)} rows recorded", flush=True)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_smoke_test())
