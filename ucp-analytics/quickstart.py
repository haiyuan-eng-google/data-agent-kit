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

"""Quickstart: stream Universal Commerce Protocol (UCP) HTTP traffic
into a partitioned, clustered BigQuery table.

Attaches a ``UCPTracker`` to an ``httpx`` async client as a response
event hook, fires two UCP requests against a mocked merchant, and
drains the writer.

Before running this sample:

  1. Set up Application Default Credentials (ADC). See
     https://cloud.google.com/docs/authentication/external/about-adc
     for instructions. The simplest path on a developer workstation
     is ``gcloud auth application-default login``.
  2. Install the runtime dependencies::

         pip install httpx google-cloud-bigquery

  3. Make sure the GCP project you pass on the command line has
     the BigQuery Data Editor role (or equivalent) for the
     service account / user behind ADC.

The sample uses ``httpx.MockTransport`` to stand in for a real UCP
merchant so it is runnable without any external HTTP dependency.
In your own code, drop the transport and point ``base_url`` at the
merchant you want to instrument.
"""

# [START ucp_analytics_quickstart]
import argparse
import asyncio

import httpx

from ucp_analytics import BQWriter, UCPTracker, make_event_hook


async def stream_events_to_bigquery(
    project_id: str,
    dataset_id: str = "ucp_analytics",
    table_id: str = "ucp_events",
) -> None:
    """Stream a couple of UCP HTTP responses into BigQuery.

    Initializes ``BQWriter`` against the caller's project, attaches a
    ``UCPTracker`` to an ``httpx`` async client as a response event
    hook, fires two UCP requests against a mocked merchant, then
    drains the writer so every buffered row is committed.

    Args:
      project_id: GCP project that owns the BigQuery dataset.
      dataset_id: BigQuery dataset; auto-created if missing.
      table_id: BigQuery table; auto-created if missing.
    """
    # Arrange.
    #
    # Initialize BQWriter once and reuse it for the lifetime of your
    # process. It owns a google-cloud-bigquery Client behind the
    # scenes; that client is safe to use from a single event loop
    # but should not be shared across processes. Always call
    # ``close()`` to drain the in-memory batch buffer before exit.
    writer = BQWriter(
        project_id=project_id,
        dataset_id=dataset_id,
        table_id=table_id,
    )
    tracker = UCPTracker(writer)

    # Mocked merchant. Replace ``httpx.MockTransport(_mock_merchant)``
    # with the real merchant base URL (and remove the ``transport``
    # kwarg) when running this against production traffic.
    transport = httpx.MockTransport(_mock_merchant)

    # Act.
    #
    # The UCP tracker is wired in as a response event hook. Every
    # request the client makes through this context manager triggers
    # ``tracker.record``, which classifies the response and enqueues
    # a row on the writer. The outer ``try / finally`` guarantees the
    # writer drains even if a request raises partway through, so any
    # already-buffered rows still reach BigQuery.
    try:
        async with httpx.AsyncClient(
            base_url="https://merchant.example.com",
            transport=transport,
            event_hooks={"response": [make_event_hook(tracker)]},
        ) as client:
            await client.get("/.well-known/ucp")
            await client.post(
                "/checkout-sessions",
                json={
                    "line_items": [
                        {"item": {"id": "roses"}, "quantity": 1},
                    ],
                },
            )
    finally:
        # Drain the buffer and release the BigQuery client. ``close``
        # flushes any rows that haven't been committed yet.
        await writer.close()

    # Assert.
    #
    # ``BQWriter`` is lossy-but-bounded by default: if BigQuery is
    # unreachable, rows stay buffered rather than dropping. A
    # non-zero ``buffered_count`` after ``close`` means the final
    # flush failed; ``last_flush_error`` carries the exception.
    if writer.buffered_count:
        raise RuntimeError(
            "Streaming insert did not drain: "
            f"{writer.buffered_count} rows still buffered "
            f"(last error: {writer.last_flush_error})"
        )
    print(f"Streamed UCP events to {writer.full_table_id}.")


def _mock_merchant(request: httpx.Request) -> httpx.Response:
    """Return a canned UCP response for the paths the quickstart hits.

    Not part of the sample surface — this exists only so the
    quickstart is self-contained against zero network dependencies.
    """
    if request.url.path == "/.well-known/ucp":
        return httpx.Response(200, json={"version": "1.0"})
    if (
        request.method == "POST"
        and request.url.path == "/checkout-sessions"
    ):
        return httpx.Response(
            201,
            json={
                "id": "chk_quickstart",
                "currency": "USD",
                "totals": [{"type": "total", "amount": 3249}],
            },
        )
    return httpx.Response(404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Stream UCP events into BigQuery (quickstart).",
    )
    parser.add_argument(
        "project_id",
        help="GCP project ID that owns the BigQuery dataset.",
    )
    parser.add_argument(
        "--dataset-id",
        default="ucp_analytics",
        help="BigQuery dataset; auto-created if missing.",
    )
    parser.add_argument(
        "--table-id",
        default="ucp_events",
        help="BigQuery table; auto-created if missing.",
    )
    args = parser.parse_args()

    asyncio.run(
        stream_events_to_bigquery(
            args.project_id,
            args.dataset_id,
            args.table_id,
        )
    )
# [END ucp_analytics_quickstart]
