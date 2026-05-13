"""Optional ADK BasePlugin adapter.

If the user is running an ADK-based commerce agent, this thin adapter
wraps UCPAnalyticsTracker into ADK's BasePlugin interface so it can
be registered on a Runner alongside the BigQuery Agent Analytics Plugin.

Install with: pip install ucp-analytics[adk]

Usage::

    from ucp_analytics.adk_plugin import UCPAgentAnalyticsPlugin

    plugin = UCPAgentAnalyticsPlugin(
        project_id="my-proj",
        dataset_id="ucp_analytics",
    )
    runner = InMemoryRunner(agent=agent, plugins=[plugin])
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from google.adk.plugins.base_plugin import BasePlugin

    _ADK_AVAILABLE = True
except ImportError:
    _ADK_AVAILABLE = False
    BasePlugin = object  # type: ignore

from ucp_analytics.events import UCPEvent, UCPEventType  # noqa: E402
from ucp_analytics.parser import UCPResponseParser  # noqa: E402
from ucp_analytics.tracker import UCPAnalyticsTracker  # noqa: E402


class UCPAgentAnalyticsPlugin(BasePlugin):  # type: ignore[misc]
    """ADK plugin that delegates to UCPAnalyticsTracker.

    Intercepts tool calls, detects UCP operations, and records
    structured commerce events. Non-UCP tool calls are optionally
    recorded as generic events.
    """

    # Tool name patterns that indicate UCP operations
    _UCP_PATTERNS = [
        "checkout",
        "create_checkout",
        "update_checkout",
        "complete_checkout",
        "cancel_checkout",
        "cart",
        "create_cart",
        "update_cart",
        "cancel_cart",
        "catalog",
        "catalog_search",
        "catalog_lookup",
        "get_product",
        "discover",
        "order",
        "identity",
        "oauth",
        "oauth2",
        "payment",
        "ucp_",
        "negotiate",
        "customer_details",
        # `simulate_shipping` is a UCP samples-server testing endpoint
        # (see `/testing/simulate-shipping/{id}` in `examples/bq_demo.py`).
        # The `_TOOL_TO_HTTP` map below already routes it, but it needs
        # a pattern match so `_is_ucp_tool` returns True and the ADK
        # callback records the event. Match the exact tool name (NOT a
        # bare "simulate" substring) so unrelated agent tools like
        # `simulate_weather` don't get pulled in as UCP traffic.
        "simulate_shipping",
    ]

    # Map ADK tool names → (HTTP method, path) for accurate event classification.
    # The classifier expects HTTP paths, not tool names.
    _TOOL_TO_HTTP: Dict[str, tuple[str, str]] = {
        "discover": ("GET", "/.well-known/ucp"),
        "discover_merchant": ("GET", "/.well-known/ucp"),
        "create_checkout": ("POST", "/checkout-sessions"),
        "update_checkout": ("PUT", "/checkout-sessions/{id}"),
        "complete_checkout": ("POST", "/checkout-sessions/{id}/complete"),
        "cancel_checkout": ("POST", "/checkout-sessions/{id}/cancel"),
        "get_checkout": ("GET", "/checkout-sessions/{id}"),
        "create_cart": ("POST", "/carts"),
        "update_cart": ("PUT", "/carts/{id}"),
        "cancel_cart": ("POST", "/carts/{id}/cancel"),
        "get_cart": ("GET", "/carts/{id}"),
        "catalog_search": ("POST", "/catalog/search"),
        "catalog_lookup": ("POST", "/catalog/lookup"),
        "get_product": ("POST", "/catalog/product"),
        "create_order": ("POST", "/orders"),
        "get_order": ("GET", "/orders/{id}"),
        "update_order": ("PUT", "/orders/{id}"),
        "order_event_webhook": ("POST", "/webhooks/partners/{id}/events/order"),
        "simulate_shipping": ("POST", "/testing/simulate-shipping/{id}"),
        "add_to_checkout": ("PUT", "/checkout-sessions/{id}"),
        "remove_from_checkout": ("PUT", "/checkout-sessions/{id}"),
        "update_customer_details": ("PUT", "/checkout-sessions/{id}"),
        "start_payment": ("PUT", "/checkout-sessions/{id}"),
    }

    def __init__(
        self,
        project_id: str,
        dataset_id: str = "ucp_analytics",
        table_id: str = "ucp_events",
        *,
        app_name: str = "",
        batch_size: int = 50,
        track_all_tools: bool = False,
        redact_pii: bool = False,
        custom_metadata: Optional[Dict[str, str]] = None,
    ):
        if not _ADK_AVAILABLE:
            raise ImportError(
                "google-adk is not installed. "
                "Install with: pip install ucp-analytics[adk]"
            )
        super().__init__(name="ucp_agent_analytics")

        self._tracker = UCPAnalyticsTracker(
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            app_name=app_name,
            batch_size=batch_size,
            redact_pii=redact_pii,
            custom_metadata=custom_metadata,
        )
        self._track_all = track_all_tools
        self._timings: Dict[str, float] = {}

    def _is_ucp_tool(self, name: str) -> bool:
        lower = name.lower()
        return any(p in lower for p in self._UCP_PATTERNS)

    async def before_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict,
        tool_context: Any,
    ) -> Optional[dict]:
        key = f"{id(tool_context)}:{tool.name}"
        self._timings[key] = time.monotonic()
        return None

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict,
        tool_context: Any,
        result: dict,
    ) -> Optional[dict]:
        key = f"{id(tool_context)}:{tool.name}"
        is_ucp = self._is_ucp_tool(tool.name)

        # Always clean up the timing entry to prevent memory leaks
        latency_ms = None
        if key in self._timings:
            latency_ms = round((time.monotonic() - self._timings.pop(key)) * 1000, 2)

        if not is_ucp and not self._track_all:
            return None

        # Classify using tool-name-to-HTTP mapping for accurate event types
        event_type_val = UCPEventType.REQUEST.value
        if is_ucp and isinstance(result, dict):
            http_method, http_path = self._TOOL_TO_HTTP.get(
                tool.name.lower(), ("POST", f"/{tool.name}")
            )
            event_type_val = UCPResponseParser.classify(
                http_method, http_path, 200, result
            ).value

        event = UCPEvent(
            event_type=event_type_val,
            app_name=getattr(tool_context, "app_name", ""),
            latency_ms=latency_ms,
        )

        # Extract UCP fields
        if is_ucp and isinstance(result, dict):
            fields = UCPResponseParser.extract(result)
            for k, v in fields.items():
                if hasattr(event, k):
                    setattr(event, k, v)

        await self._tracker.record_event(event)
        return None

    async def close(self):
        await self._tracker.close()
