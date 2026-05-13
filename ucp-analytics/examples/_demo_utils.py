"""Shared BigQuery demo infrastructure for UCP Analytics examples.

Provides:
- Shared config (PROJECT_ID, DATASET_ID, TABLE_ID)
- create_tracker() — returns a UCPAnalyticsTracker wired to BigQuery
- verify_bigquery() — queries BQ after streaming buffer delay
- print_bq_results() — formats BQ query results

All demo scripts import from here to avoid code duplication.
"""

from __future__ import annotations

import asyncio
import os

from ucp_analytics import UCPAnalyticsTracker

# ======================================================================
# BigQuery configuration
# Set GCP_PROJECT_ID env var, or edit the fallback below.
# ======================================================================

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "your-gcp-project-id")
DATASET_ID = "ucp_analytics"
TABLE_ID = "ucp_events"


def create_tracker(app_name: str) -> UCPAnalyticsTracker:
    """Create a UCPAnalyticsTracker configured for BigQuery."""
    return UCPAnalyticsTracker(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        table_id=TABLE_ID,
        app_name=app_name,
        batch_size=1,  # Flush every event for demos
        auto_create_table=True,
    )


async def verify_bigquery(app_name: str, label: str = "Analytics Report") -> None:
    """Query BigQuery to verify demo events landed correctly."""
    from google.cloud import bigquery

    print("\n" + "=" * 70)
    print(f"  BIGQUERY VERIFICATION — {label}")
    print("=" * 70)

    client = bigquery.Client(project=PROJECT_ID)
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"

    print("\n   Waiting 10s for BigQuery streaming buffer...")
    await asyncio.sleep(10)

    query = f"""
    SELECT event_type, transport, checkout_status, total_amount,
           latency_ms, http_method, http_status_code
    FROM `{table_ref}`
    WHERE app_name = '{app_name}'
    ORDER BY timestamp
    """
    print(f"   Querying BigQuery for app_name='{app_name}'...")

    rows = list(client.query(query).result())
    print_bq_results(rows)
    client.close()


def print_bq_results(rows: list) -> None:
    """Format and print BigQuery query results."""
    if not rows:
        print("\n   WARNING: No rows found in BigQuery yet.")
        print("   Streaming inserts may take up to 90 seconds to be queryable.")
        return

    print(f"\n   Found {len(rows)} events in BigQuery:")
    print(
        f"   {'#':<3} {'Event Type':<32} {'Transport':<8} {'Status':<22} "
        f"{'Total':>9} {'Latency':>8}"
    )
    print("   " + "-" * 87)

    for i, row in enumerate(rows, 1):
        total_str = f"${row.total_amount / 100:.2f}" if row.total_amount else ""
        status_str = row.checkout_status or ""
        latency_str = f"{row.latency_ms:.0f}ms" if row.latency_ms else ""
        transport_str = row.transport or "rest"
        print(
            f"   {i:<3} {row.event_type:<32} {transport_str:<8} {status_str:<22} "
            f"{total_str:>9} {latency_str:>8}"
        )

    # Event type summary
    event_types: dict[str, int] = {}
    for row in rows:
        event_types[row.event_type] = event_types.get(row.event_type, 0) + 1

    print("\n   Event types captured:")
    for et, cnt in sorted(event_types.items()):
        print(f"     {et:<40} {cnt:>3}")
    print(f"\n   Total events: {len(rows)}")
