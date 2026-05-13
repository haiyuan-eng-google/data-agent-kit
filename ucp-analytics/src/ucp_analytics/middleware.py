"""FastAPI / Starlette ASGI middleware for UCP merchant servers.

Drop this onto the UCP samples server (or any FastAPI-based UCP business
server) to automatically capture every checkout-session, order, and
discovery request into BigQuery.

Usage::

    from fastapi import FastAPI
    from ucp_analytics import UCPAnalyticsTracker, UCPAnalyticsMiddleware

    app = FastAPI()
    tracker = UCPAnalyticsTracker(project_id="my-proj", app_name="flower_shop")
    app.add_middleware(UCPAnalyticsMiddleware, tracker=tracker)

    @app.on_event("shutdown")
    async def shutdown():
        await tracker.close()  # drains in-flight tasks, then flushes
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from ucp_analytics._path_match import is_webhook_delivery, path_matches_marker

logger = logging.getLogger(__name__)


class UCPAnalyticsMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that intercepts UCP HTTP traffic on the server side.

    For every request whose path looks like a UCP operation
    (/checkout-sessions, /.well-known/ucp, /orders, etc.) the middleware:

    1. Reads the request body
    2. Lets the handler execute normally
    3. Reads the response body
    4. Passes both to UCPAnalyticsTracker.record_http()

    Non-UCP paths are passed through with zero overhead.
    """

    # Paths that indicate UCP traffic
    UCP_PATH_PREFIXES = (
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
        "/testing/simulate",
        "/webhooks",
        "/webhook",
    )

    def __init__(self, app: Any, tracker: Any) -> None:
        super().__init__(app)
        self.tracker = tracker

    def _accepts(self, path: str, request_headers: Any) -> bool:
        """Decide whether to capture a request.

        Two acceptance branches:
          1. The path matches a known UCP marker segment
             (`/checkout-sessions`, `/orders`, `/.well-known/ucp`, etc.)
             OR an operator-configured webhook prefix on the tracker.
          2. The request looks like a webhook delivery by headers
             (Standard Webhooks `Webhook-Id` + `Webhook-Timestamp`),
             routing on a platform-specific URL the operator did not
             enumerate. UCP `order.md`: *"The URL format is
             platform-specific."*

        Configuration lives on the tracker (`tracker.webhook_path_prefixes`)
        — a single source of truth, so an operator who configures a
        platform's webhook prefix once gets it applied at every
        integration that wraps the same tracker (server middleware,
        agent-side HTTPX hook). Header-based detection has its own
        suppression for known UCP REST paths (see
        `_path_match.is_webhook_delivery`) so a buggy or malicious
        sender can't stamp webhook headers onto `/checkout-sessions`
        and falsely route into the order-webhook branch.
        """
        extras = tuple(getattr(self.tracker, "webhook_path_prefixes", ()) or ())
        if any(
            path_matches_marker(path, p) for p in (*self.UCP_PATH_PREFIXES, *extras)
        ):
            return True
        return is_webhook_delivery(path, request_headers, extras)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Fast path: skip non-UCP requests. UCP REST paths in the spec are
        # relative to the platform-advertised base endpoint, so a real
        # deployment can mount any of the marker segments under a prefix
        # like /ucp/v1, /api/v2, /merchant/api/ucp/v1, etc. Use a
        # segment-aware match so /api/catalogue/search doesn't trip the
        # /catalog marker and /api/orders-history doesn't trip /orders.
        # Header-based fallback (Standard Webhooks Webhook-Id +
        # Webhook-Timestamp) catches platform-specific webhook URLs the
        # operator hasn't enumerated.
        if not self._accepts(path, request.headers):
            return await call_next(request)

        # Read request body (for POST/PUT)
        request_body = None
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                raw = await request.body()
                if raw:
                    request_body = json.loads(raw)
            except Exception:
                pass

        # Execute the actual handler
        start = time.monotonic()
        response = await call_next(request)
        latency_ms = round((time.monotonic() - start) * 1000, 2)

        # Read response body
        response_body = None
        body_bytes = b""
        async for chunk in response.body_iterator:
            if isinstance(chunk, str):
                body_bytes += chunk.encode("utf-8")
            else:
                body_bytes += chunk

        try:
            if body_bytes:
                response_body = json.loads(body_bytes)
        except Exception:
            pass

        # Record the event (fire-and-forget; don't block the response).
        # Tasks are tracked on the tracker so tracker.close() drains them.
        try:
            headers = dict(request.headers)
            response_headers = dict(response.headers)
            # RFC 7235 §4.1 permits multiple WWW-Authenticate field
            # lines on a single response (a Basic and a Bearer
            # challenge often appear on separate lines). Starlette's
            # MutableHeaders preserves them, but `dict(...)` collapses
            # to the first occurrence — which would silently lose the
            # Bearer challenge on a Basic-then-Bearer response and
            # leave auth_challenge_* null. Re-merge them with a `, `
            # separator so parse_bearer_challenge() sees both.
            www_auth_values = response.headers.getlist("www-authenticate")
            if len(www_auth_values) > 1:
                response_headers["www-authenticate"] = ", ".join(www_auth_values)
            task = asyncio.create_task(
                self.tracker.record_http(
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
            )
            self.tracker.register_pending_task(task)
        except Exception:
            logger.exception("UCP analytics recording failed")

        # Re-create the response with the consumed body, preserving raw headers
        from starlette.responses import Response as StarletteResponse

        new_response = StarletteResponse(
            content=body_bytes,
            status_code=response.status_code,
            media_type=response.media_type,
        )
        # Preserve all original headers including multi-value ones (e.g. set-cookie)
        new_response.raw_headers = response.raw_headers
        return new_response

    async def drain_pending(self) -> None:
        """Await all in-flight recording tasks.

        .. deprecated::
            Pending tasks are now tracked on the tracker itself.
            ``tracker.close()`` drains automatically.  This method
            delegates to ``self.tracker.drain_pending()`` for
            backwards compatibility.
        """
        await self.tracker.drain_pending()
