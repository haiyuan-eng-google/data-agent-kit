"""Tests for UCPAnalyticsMiddleware path filtering.

Lightweight tests that bypass FastAPI/Starlette wiring and exercise the
detector logic directly. The full middleware integration is exercised
end-to-end elsewhere; these tests pin the behavior the PR-9 reviewer
flagged: the server-side detector must accept mounted UCP base paths
the same way the HTTPX client hook already does, without falsely
matching segment-prefix lookalikes like /api/catalogue/search.
"""

from __future__ import annotations

import pytest

# starlette ships only in the [fastapi] extra; tests that exercise the
# middleware module skip cleanly in dev-only environments.
pytest.importorskip("starlette")

from ucp_analytics._path_match import path_matches_marker  # noqa: E402
from ucp_analytics.middleware import UCPAnalyticsMiddleware  # noqa: E402


def _matches(path: str) -> bool:
    """Replicates the dispatch() fast-path filter for UCPAnalyticsMiddleware."""
    return any(
        path_matches_marker(path, p) for p in UCPAnalyticsMiddleware.UCP_PATH_PREFIXES
    )


class TestMiddlewareDetectorMatchesMountedPaths:
    """OpenAPI paths in the UCP spec are relative to the platform-advertised
    REST endpoint, so real deployments mount the marker segments under a
    prefix. The middleware detector must accept these the same way the
    HTTPX hook does — without this, server-side traffic at any non-root
    mount silently bypasses analytics."""

    def test_root_catalog_matches(self):
        assert _matches("/catalog/search")
        assert _matches("/catalog/lookup")
        assert _matches("/catalog/product")

    def test_mounted_catalog_matches(self):
        assert _matches("/ucp/v1/catalog/search")
        assert _matches("/api/v2/catalog/lookup")
        assert _matches("/merchant/api/ucp/v1/catalog/product")

    def test_mounted_checkout_matches(self):
        assert _matches("/ucp/v1/checkout-sessions")
        assert _matches("/api/v2/checkout-sessions/chk_abc/complete")

    def test_mounted_carts_matches(self):
        assert _matches("/ucp/v1/carts")
        assert _matches("/merchant/api/ucp/v1/carts/cart_abc")

    def test_mounted_orders_matches(self):
        assert _matches("/ucp/v1/orders/order_abc")

    def test_mounted_identity_matches(self):
        assert _matches("/ucp/v1/identity")
        assert _matches("/api/v2/identity/callback")

    def test_mounted_oauth2_matches(self):
        # OAuth flow endpoints — segment-aware match works even when
        # the base path is something like /api/v1.
        assert _matches("/oauth2/authorize")
        assert _matches("/oauth2/token")
        assert _matches("/oauth2/revoke")
        assert _matches("/oauth2/jwks")
        assert _matches("/api/v1/oauth2/token")
        assert _matches("/merchant/api/ucp/v1/oauth2/authorize")

    def test_oauth_metadata_discovery_matches(self):
        assert _matches("/.well-known/oauth-authorization-server")
        assert _matches("/.well-known/openid-configuration")
        assert _matches("/.well-known/oauth-protected-resource")
        assert _matches("/api/v1/.well-known/oauth-authorization-server")

    def test_unrelated_paths_do_not_match(self):
        assert not _matches("/healthz")
        assert not _matches("/api/users")
        assert not _matches("/static/main.css")
        assert not _matches("/")


class TestMiddlewareDetectorRejectsNearMisses:
    """Segment-prefix lookalikes that share a substring with a UCP marker
    must not match. A loose `marker in path` filter would record these as
    UCP traffic, then consume their bodies and emit noisy generic events."""

    def test_catalog_does_not_match_catalogue(self):
        assert not _matches("/api/catalogue/search")
        assert not _matches("/v1/catalogue")

    def test_orders_does_not_match_orders_history(self):
        assert not _matches("/api/orders-history")
        assert not _matches("/api/reorders")

    def test_identity_does_not_match_identity_card(self):
        assert not _matches("/api/identity-card")
        assert not _matches("/api/identitymanager")

    def test_carts_does_not_match_carts_preview(self):
        assert not _matches("/api/carts-preview")
        assert not _matches("/api/discards")

    def test_checkout_sessions_does_not_match_lookalike(self):
        assert not _matches("/api/checkout-sessions-archive")

    def test_oauth2_does_not_match_oauth2_proxy(self):
        # `/oauth2-proxy` is a real piece of infra (oauth2-proxy
        # reverse proxy); the marker must not catch it.
        assert not _matches("/api/oauth2-proxy/start")
        assert not _matches("/oauth2-debug")


# Direct unit coverage of path_matches_marker lives in tests/test_path_match.py
# so it runs even in environments without the optional Starlette dependency.


# ---------------------------------------------------------------------- #
# Middleware integration tests
# ---------------------------------------------------------------------- #
# These exercise dispatch() through Starlette's TestClient so we can
# verify how the middleware shapes headers (specifically, multi-value
# WWW-Authenticate preservation) before they reach record_http().


class TestMiddlewareMultiValueResponseHeaders:
    """RFC 7235 §4.1 permits multiple WWW-Authenticate field lines.
    Starlette's MutableHeaders preserves them, but `dict(...)` flattens
    to one — which would lose the Bearer challenge if Basic appears on
    an earlier line. Pin that the middleware re-merges them so
    parse_bearer_challenge() can find Bearer downstream."""

    def _build_app(self, mock_tracker, www_authenticate_lines):
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route

        from ucp_analytics.middleware import UCPAnalyticsMiddleware

        async def auth_failure(request):
            response = Response(status_code=401, content=b"")
            # Use append-style so each line is a distinct field line —
            # MutableHeaders.__setitem__ would replace.
            del response.headers["www-authenticate"]
            for line in www_authenticate_lines:
                response.headers.append("WWW-Authenticate", line)
            return response

        app = Starlette(routes=[Route("/orders/{id}", auth_failure)])
        app.add_middleware(UCPAnalyticsMiddleware, tracker=mock_tracker)
        return app

    @pytest.fixture
    def mock_tracker(self):
        from unittest.mock import AsyncMock, MagicMock

        tracker = MagicMock()
        tracker.record_http = AsyncMock()
        # register_pending_task is called synchronously in dispatch();
        # provide a no-op implementation so it doesn't raise.
        tracker.register_pending_task = MagicMock()
        return tracker

    def test_basic_then_bearer_two_field_lines_preserves_bearer(self, mock_tracker):
        from starlette.testclient import TestClient

        from ucp_analytics._headers import parse_bearer_challenge

        app = self._build_app(
            mock_tracker,
            [
                'Basic realm="legacy"',
                'Bearer realm="merchant", error="invalid_token"',
            ],
        )
        with TestClient(app) as client:
            client.get("/orders/order_123")

        mock_tracker.record_http.assert_awaited_once()
        response_headers = mock_tracker.record_http.call_args.kwargs["response_headers"]
        # The Bearer challenge survives the dict() collapse and is
        # reachable through the parser despite Basic being on an
        # earlier field line.
        params = parse_bearer_challenge(response_headers)
        assert params == {
            "realm": "merchant",
            "error": "invalid_token",
        }

    def test_single_www_authenticate_passes_through_unchanged(self, mock_tracker):
        """The merge logic only kicks in when there are multiple field
        lines; the single-line case stays as-is so we don't double-
        encode commas in well-formed senders."""
        from starlette.testclient import TestClient

        app = self._build_app(
            mock_tracker,
            ['Bearer realm="merchant", error="invalid_token"'],
        )
        with TestClient(app) as client:
            client.get("/orders/order_123")

        mock_tracker.record_http.assert_awaited_once()
        response_headers = mock_tracker.record_http.call_args.kwargs["response_headers"]
        # Same value as the original — no extra processing.
        assert (
            response_headers["www-authenticate"]
            == 'Bearer realm="merchant", error="invalid_token"'
        )


class TestMiddlewareWebhookPathPrefixes:
    """B5b — UCP order.md says webhook URL format is platform-specific.
    Configuration lives on the tracker (`tracker.webhook_path_prefixes`)
    so a single source of truth applies to capture (middleware path
    filter) AND classification (tracker / parser). Without that, the
    middleware would capture `/events` but the tracker would emit
    event_type=request instead of order_*."""

    @pytest.fixture
    def mock_tracker(self):
        from unittest.mock import AsyncMock, MagicMock

        tracker = MagicMock()
        tracker.record_http = AsyncMock()
        tracker.register_pending_task = MagicMock()
        # Default: no configured webhook prefixes. Per-test overrides
        # set this explicitly to simulate operator configuration.
        tracker.webhook_path_prefixes = ()
        return tracker

    def test_default_prefixes_skip_events_path(self, mock_tracker):
        """Without operator config, `/events` is not a UCP path —
        the middleware skips it entirely (zero overhead, no
        record_http call)."""
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from ucp_analytics.middleware import UCPAnalyticsMiddleware

        async def handler(request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/events", handler, methods=["POST"])])
        app.add_middleware(UCPAnalyticsMiddleware, tracker=mock_tracker)

        with TestClient(app) as client:
            client.post("/events", json={"id": "order_x", "status": "shipped"})

        mock_tracker.record_http.assert_not_awaited()

    def test_tracker_configured_prefix_captures_events_path(self, mock_tracker):
        """With `tracker.webhook_path_prefixes=('/events',)`, the
        middleware accepts the path and flows into record_http."""
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from ucp_analytics.middleware import UCPAnalyticsMiddleware

        mock_tracker.webhook_path_prefixes = ("/events",)

        async def handler(request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/events", handler, methods=["POST"])])
        app.add_middleware(UCPAnalyticsMiddleware, tracker=mock_tracker)

        with TestClient(app) as client:
            client.post("/events", json={"id": "order_x", "status": "shipped"})

        mock_tracker.record_http.assert_awaited_once()
        call_kwargs = mock_tracker.record_http.call_args.kwargs
        assert call_kwargs["path"] == "/events"

    def test_tracker_configured_prefix_handles_mounted_path(self, mock_tracker):
        """Tracker-configured `/events` works under a mount, same as
        the default UCP markers."""
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from ucp_analytics.middleware import UCPAnalyticsMiddleware

        mock_tracker.webhook_path_prefixes = ("/events",)

        async def handler(request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/api/v1/events", handler, methods=["POST"])])
        app.add_middleware(UCPAnalyticsMiddleware, tracker=mock_tracker)

        with TestClient(app) as client:
            client.post("/api/v1/events", json={"id": "order_x", "status": "delivered"})

        mock_tracker.record_http.assert_awaited_once()

    def test_header_pair_captures_unknown_path(self, mock_tracker):
        """Reviewer's High #1 repro: a `/events` request carrying
        Standard Webhooks `Webhook-Id` + `Webhook-Timestamp` headers
        must be captured even when no operator prefix is configured.
        Without this fallback in the middleware's capture gate,
        header-based webhook detection inside the tracker / classifier
        never gets a chance to fire — the request is skipped at the
        path filter."""
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from ucp_analytics.middleware import UCPAnalyticsMiddleware

        async def handler(request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/events", handler, methods=["POST"])])
        app.add_middleware(UCPAnalyticsMiddleware, tracker=mock_tracker)

        with TestClient(app) as client:
            client.post(
                "/events",
                json={"id": "order_x", "status": "delivered"},
                headers={
                    "Webhook-Id": "evt_42",
                    "Webhook-Timestamp": "1767225600",
                },
            )

        mock_tracker.record_http.assert_awaited_once()

    def test_header_pair_does_not_capture_unrelated_path(self, mock_tracker):
        """Header-based capture must stay scoped: a request to an
        unrelated /api/foo path WITHOUT webhook headers stays
        skipped. Pin this to make sure the new acceptance branch
        doesn't accidentally widen capture beyond UCP+webhook."""
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from ucp_analytics.middleware import UCPAnalyticsMiddleware

        async def handler(request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/api/foo", handler, methods=["POST"])])
        app.add_middleware(UCPAnalyticsMiddleware, tracker=mock_tracker)

        with TestClient(app) as client:
            client.post("/api/foo", json={})

        mock_tracker.record_http.assert_not_awaited()
