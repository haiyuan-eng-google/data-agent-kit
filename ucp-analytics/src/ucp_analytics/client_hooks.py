"""HTTPX event hook for UCP platform / agent clients.

Attach to an httpx.AsyncClient to automatically capture every outgoing
UCP request and incoming response into BigQuery analytics.

Usage::

    import httpx
    from ucp_analytics import UCPAnalyticsTracker, UCPClientEventHook

    tracker = UCPAnalyticsTracker(project_id="my-proj", app_name="shopping_agent")
    hook = UCPClientEventHook(tracker)

    client = httpx.AsyncClient(
        event_hooks={"response": [hook]},
    )

    # Every call to the UCP merchant is now tracked:
    resp = await client.post(
        "https://merchant.example.com/checkout-sessions",
        json={"line_items": [...]},
    )
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from ucp_analytics._path_match import is_webhook_delivery, path_matches_marker

logger = logging.getLogger(__name__)


class UCPClientEventHook:
    """HTTPX response event hook that records UCP analytics.

    This hook fires after every HTTP response. It checks whether the
    request path looks like a UCP operation and, if so, records both
    the request and response into BigQuery.
    """

    UCP_PATH_PATTERNS = (
        "/checkout-sessions",
        "/carts",
        "/catalog",
        "/.well-known/ucp",
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
        "/.well-known/oauth-protected-resource",
        "/orders",
        "/identity",
        "/oauth2",
        "/simulate-shipping",
        "/webhooks",
        "/webhook",
    )

    def __init__(self, tracker: Any) -> None:
        self.tracker = tracker

    def _accepts(self, path: str, request_headers: Any) -> bool:
        """Decide whether to capture a response.

        Mirrors `UCPAnalyticsMiddleware._accepts`. Two acceptance
        branches: known UCP marker (incl. tracker-configured webhook
        prefixes) OR Standard Webhooks header pair on a
        platform-specific URL. Configuration lives on the tracker
        (`tracker.webhook_path_prefixes`) so a single source of truth
        applies to both the server middleware and this agent-side
        hook when they wrap the same tracker.
        """
        extras = tuple(getattr(self.tracker, "webhook_path_prefixes", ()) or ())
        if any(
            path_matches_marker(path, p) for p in (*self.UCP_PATH_PATTERNS, *extras)
        ):
            return True
        return is_webhook_delivery(path, request_headers, extras)

    async def __call__(self, response: httpx.Response) -> None:
        """Called by HTTPX after each response is received."""
        request = response.request
        path = request.url.path

        # Skip non-UCP requests. UCP REST paths in the spec are relative
        # to the platform-advertised base endpoint, so a real merchant can
        # mount the marker segments under a prefix like /ucp/v1, /api/v2.
        # Use the segment-aware helper so /api/catalogue/search,
        # /api/orders-history, etc. don't trip the filter. Header-based
        # fallback (Standard Webhooks Webhook-Id + Webhook-Timestamp)
        # catches platform-specific webhook URLs not in the path set.
        if not self._accepts(path, request.headers):
            return

        # Read response body
        await response.aread()

        response_body = None
        try:
            response_body = response.json()
        except Exception:
            pass

        # Read request body
        request_body = None
        if request.content:
            try:
                request_body = json.loads(request.content)
            except Exception:
                pass

        # Latency (approximate — from request creation to response received)
        latency_ms = None
        elapsed = response.elapsed
        if elapsed:
            latency_ms = round(elapsed.total_seconds() * 1000, 2)

        # Record
        try:
            headers = dict(request.headers)
            response_headers = dict(response.headers)
            # RFC 7235 §4.1 permits multiple WWW-Authenticate field
            # lines on a single response. httpx.Headers preserves them,
            # but `dict(...)` collapses to one — which would silently
            # lose the Bearer challenge on a multi-scheme response and
            # leave auth_challenge_* null. Re-merge with `, ` so
            # parse_bearer_challenge() sees both. (Same fix shape as
            # UCPAnalyticsMiddleware.)
            www_auth_values = response.headers.get_list("www-authenticate")
            if len(www_auth_values) > 1:
                response_headers["www-authenticate"] = ", ".join(www_auth_values)
            await self.tracker.record_http(
                method=request.method,
                url=str(request.url),
                path=path,
                status_code=response.status_code,
                request_body=request_body,
                response_body=response_body,
                latency_ms=latency_ms,
                request_headers=headers,
                response_headers=response_headers,
            )
        except Exception:
            logger.exception("UCP client analytics recording failed")


class UCPClientTransport(httpx.AsyncBaseTransport):
    """Optional: wrapping transport that adds timing to every request.

    For most users, the event hook above is sufficient. This transport
    wrapper adds precise timing that httpx.Response.elapsed might miss
    for streaming responses.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport, tracker: Any):
        self._transport = transport
        self.tracker = tracker

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        start = time.monotonic()
        response = await self._transport.handle_async_request(request)
        latency_ms = round((time.monotonic() - start) * 1000, 2)

        # Attach latency for the event hook to pick up
        response.extensions["ucp_latency_ms"] = latency_ms
        return response
