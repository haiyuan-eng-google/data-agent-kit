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

"""smoke_test — regression test for ucp_analytics.py.

This file is **not** a code sample. The canonical runnable sample
is ``quickstart.py``. ``smoke_test.py`` has a multi-mode CLI and a
synthetic event corpus because its job is to assert spec coverage
on every push, which a single-purpose sample cannot do.

Two modes, both driven by the same event generator:

  * Default (no args): writes rows to stdout via ``PrintWriter``.
    No GCP credentials needed; asserts all 32 UCP spec events appear.
  * ``--e2e --project-id PID``: streams rows through ``BQWriter``
    (from ``ucp_analytics``) into a real BigQuery table, auto-created.
    With ``--verify``, polls the table back afterwards to confirm
    rows are queryable.

Run:

    python smoke_test.py
    python smoke_test.py --e2e --project-id YOUR_GCP_PROJECT --verify

When you copy ``ucp_analytics.py`` and ``sample_agent.py`` into
your own project, leave this file behind.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple

import httpx

from sample_agent import SampleAgent
from ucp_analytics import BQWriter, UCPTracker

try:
    from google.cloud import bigquery
except ImportError:  # pragma: no cover — keep stdout mode usable.
    bigquery = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Writers used only by the smoke test
# ---------------------------------------------------------------------------

class PrintWriter:
    """Drop-in stand-in for BQWriter that prints rows as JSON lines."""

    async def enqueue(self, row: dict) -> None:
        import json
        print(json.dumps(row))

    async def flush(self) -> None:  # pragma: no cover
        pass

    async def close(self) -> None:  # pragma: no cover
        pass


class CoverageTeeWriter:
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


# All 32 UCP spec event types — the smoke test exercises every one
# and asserts the coverage at exit.
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


# ---------------------------------------------------------------------------
# Event generator — shared by stdout and e2e modes
# ---------------------------------------------------------------------------

async def smoke_test(
    writer: Any, *, merchant_tag: str = "shop.example.com",
) -> None:
    """Drive 33 fake responses + agent calls through ``writer``.

    Parametric on writer so the same generator backs both the stdout
    smoke (``PrintWriter``) and the BigQuery e2e (``BQWriter``).

    ``merchant_tag`` is used as the HTTPX host AND as the
    ``merchant_host`` argument to every agent call, so e2e mode can
    filter the table back down to the rows from this specific run.
    """
    tracker = UCPTracker(writer)  # type: ignore[arg-type]
    agent = SampleAgent(tracker)

    host = f"https://{merchant_tag}"

    # 27 HTTPX-visible samples (one per parser-derivable event type,
    # including ``order_webhook_received`` from inbound /webhook(s)
    # traffic; the agent emits its own copy below for parity).
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

    # 6 SampleAgent calls (5 agent-only event types + 1 overlap).
    # The spec has 32 types total: 27 are parser-derivable from
    # HTTPX traffic above; 5 fire only inside the agent loop
    # (capability_negotiated, payment_handler_negotiated,
    # payment_instrument_selected, payment_completed, payment_failed).
    # ``order_webhook_received`` is the overlap — already emitted by
    # the parser from /webhooks/orders above, and exercised again
    # here because real server-side handlers go through SampleAgent.
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


def check_coverage(seen: set, row_count: int) -> None:
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


# ---------------------------------------------------------------------------
# Mode runners
# ---------------------------------------------------------------------------

async def run_print() -> None:
    """Default mode: print rows to stdout, no GCP creds."""
    tee = CoverageTeeWriter(PrintWriter())
    await smoke_test(tee)
    check_coverage(tee.seen_types, tee.row_count)


async def run_e2e(
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
    tee = CoverageTeeWriter(writer)

    print(f"# e2e mode: streaming to {writer.full_table_id}", flush=True)
    print(f"# run tag: merchant_host = {merchant_tag!r}", flush=True)

    try:
        await smoke_test(tee, merchant_tag=merchant_tag)
    finally:
        await writer.close()  # drain + flush whatever's buffered

    # Refuse to report success if the final flush couldn't drain. The
    # tee writer only counts rows that were *handed to* BQWriter, not
    # rows BigQuery actually accepted; without this check, a bad
    # project / missing IAM / network failure would silently print
    # "32/32 distinct event types" while zero rows landed.
    if writer.buffered_count > 0:
        err = writer.last_flush_error
        err_msg = (
            f" (last flush error: {type(err).__name__}: {err})"
            if err is not None else ""
        )
        raise SystemExit(
            f"flush failed: {writer.buffered_count} of {tee.row_count} rows "
            f"still buffered after close — BigQuery did not accept them"
            f"{err_msg}"
        )

    check_coverage(tee.seen_types, tee.row_count)

    if verify:
        await verify_e2e(
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            merchant_tag=merchant_tag,
            run_start_iso=run_start,
            expected=tee.row_count,
            timeout_s=verify_timeout_s,
        )


async def verify_e2e(
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


def main() -> None:
    """CLI entry point. ``python smoke_test.py`` for stdout smoke;
    ``python smoke_test.py --e2e --project-id PID`` for BigQuery."""
    import argparse
    p = argparse.ArgumentParser(
        prog="smoke_test",
        description=(
            "Standalone smoke test for ucp_analytics. With no args, "
            "prints rows to stdout and asserts all 32 UCP spec events "
            "are emitted. With --e2e, streams the same rows to a real "
            "BigQuery table for end-to-end verification."
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
        asyncio.run(run_e2e(
            project_id=args.project_id,
            dataset_id=args.dataset_id,
            table_id=args.table_id,
            verify=args.verify,
            verify_timeout_s=args.verify_timeout,
        ))
    else:
        asyncio.run(run_print())


if __name__ == "__main__":  # pragma: no cover
    main()
