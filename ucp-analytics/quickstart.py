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

# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-cloud-bigquery>=3.20.0",
#   "httpx>=0.27.0",
# ]
# ///

"""Quickstart: stream Universal Commerce Protocol (UCP) analytics events
into a partitioned, clustered BigQuery table.

Demonstrates the canonical agent-emission pattern: create a
``BQWriter``, wrap it in a ``UCPTracker``, then call methods on
``SampleAgent`` at each decision moment in your agent loop.

Run::

    # 1. Authenticate.
    gcloud auth application-default login

    # 2. Run directly (PEP 723 dependency block lets uv install the
    #    runtime dep on the fly):
    uv run quickstart.py YOUR_GCP_PROJECT

    # Or with pip:
    pip install httpx google-cloud-bigquery
    python quickstart.py YOUR_GCP_PROJECT

See https://cloud.google.com/docs/authentication/external/about-adc
for Application Default Credentials setup.
"""

# [START ucp_analytics_quickstart]
import argparse
import asyncio

from sample_agent import SampleAgent
from ucp_analytics import BQWriter, UCPTracker


async def stream_events_to_bigquery(
    project_id: str,
    dataset_id: str = "ucp_analytics",
    table_id: str = "ucp_events",
) -> None:
    """Stream a handful of UCP analytics events into BigQuery.

    Initializes BQWriter against the caller's project, wires it
    into a UCPTracker + SampleAgent, emits three agent-decision
    events, then drains the writer so every buffered row is
    committed.

    Args:
      project_id: GCP project that owns the BigQuery dataset.
      dataset_id: BigQuery dataset; auto-created if missing.
      table_id: BigQuery table; auto-created if missing.
    """
    # Initialize the writer once and reuse it for the lifetime of
    # your process. It owns a google-cloud-bigquery Client behind
    # the scenes; that client is safe to use from a single event
    # loop. Always close() before exit to drain the in-memory
    # batch buffer.
    writer = BQWriter(
        project_id=project_id,
        dataset_id=dataset_id,
        table_id=table_id,
    )
    tracker = UCPTracker(writer)
    agent = SampleAgent(tracker)

    # The try/finally guarantees the writer drains even if one of
    # the emissions raises partway through, so already-buffered
    # rows still reach BigQuery.
    try:
        # Each await emits one row. Wire these calls into the
        # corresponding decision points of your real agent loop.
        await agent.capability_negotiated(
            merchant_host="merchant.example.com",
        )
        await agent.payment_handler_negotiated(
            checkout_session_id="chk_quickstart",
            merchant_host="merchant.example.com",
        )
        await agent.payment_completed(
            checkout_session_id="chk_quickstart",
            order_id="order_quickstart",
            currency="USD",
            total_amount=3249,
            merchant_host="merchant.example.com",
        )
    finally:
        await writer.close()

    # BQWriter is lossy-but-bounded by default: if BigQuery is
    # unreachable, rows stay buffered rather than dropping. A
    # non-zero buffered_count after close() means the final flush
    # failed; last_flush_error carries the underlying exception.
    if writer.buffered_count:
        raise RuntimeError(
            "Streaming insert did not drain: "
            f"{writer.buffered_count} rows still buffered "
            f"(last error: {writer.last_flush_error})"
        )
    print(f"Streamed UCP events to {writer.full_table_id}.")


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
