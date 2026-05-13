"""UCP Analytics — commerce event tracking for the Universal Commerce Protocol.

Captures checkout sessions, order lifecycle, payment, identity linking, and
capability negotiation events. Writes structured rows to BigQuery for funnel
analysis, error debugging, and revenue attribution.

Integration points (pick any or combine):
    1. FastAPI middleware  — wraps a UCP merchant server
    2. HTTPX transport     — wraps a UCP platform client
    3. ADK plugin          — wraps an ADK commerce agent (optional)
    4. Direct API          — call tracker.record() from any code
"""

from ucp_analytics.client_hooks import UCPClientEventHook
from ucp_analytics.events import CheckoutStatus, UCPEvent, UCPEventType
from ucp_analytics.parser import UCPResponseParser
from ucp_analytics.tracker import UCPAnalyticsTracker
from ucp_analytics.writer import AsyncBigQueryWriter


def __getattr__(name: str):
    if name == "UCPAnalyticsMiddleware":
        from ucp_analytics.middleware import UCPAnalyticsMiddleware

        return UCPAnalyticsMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "UCPAnalyticsTracker",
    "UCPEvent",
    "UCPEventType",
    "CheckoutStatus",
    "UCPResponseParser",
    "AsyncBigQueryWriter",
    "UCPAnalyticsMiddleware",
    "UCPClientEventHook",
]

__version__ = "0.2.0"
