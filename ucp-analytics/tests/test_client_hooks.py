"""Tests for UCPClientEventHook."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ucp_analytics.client_hooks import UCPClientEventHook


@pytest.fixture
def mock_tracker():
    tracker = MagicMock()
    tracker.record_http = AsyncMock()
    return tracker


@pytest.fixture
def hook(mock_tracker):
    return UCPClientEventHook(mock_tracker)


def _make_response(
    url: str = "https://shop.example.com/checkout-sessions",
    method: str = "POST",
    status_code: int = 201,
    json_body: dict | None = None,
    request_content: bytes = b"",
    request_headers: dict | None = None,
    response_headers: dict | None = None,
) -> httpx.Response:
    """Build a mock httpx.Response."""
    request = httpx.Request(
        method, url, content=request_content, headers=request_headers or {}
    )
    response = httpx.Response(
        status_code=status_code,
        request=request,
        json=json_body,
        headers=response_headers or {},
    )
    response._elapsed = timedelta(milliseconds=42)
    return response


class TestUCPClientEventHook:
    async def test_records_ucp_request(self, hook, mock_tracker):
        resp = _make_response(
            url="https://shop.example.com/checkout-sessions",
            method="POST",
            status_code=201,
            json_body={"id": "chk_123"},
        )

        await hook(resp)

        mock_tracker.record_http.assert_awaited_once()
        call_kwargs = mock_tracker.record_http.call_args.kwargs
        assert call_kwargs["method"] == "POST"
        assert call_kwargs["status_code"] == 201
        assert call_kwargs["response_body"] == {"id": "chk_123"}

    async def test_skips_non_ucp_request(self, hook, mock_tracker):
        resp = _make_response(
            url="https://shop.example.com/api/health",
            method="GET",
            status_code=200,
        )

        await hook(resp)

        mock_tracker.record_http.assert_not_awaited()

    async def test_captures_discovery(self, hook, mock_tracker):
        resp = _make_response(
            url="https://shop.example.com/.well-known/ucp",
            method="GET",
            status_code=200,
            json_body={"ucp": {"version": "2026-01-11"}},
        )

        await hook(resp)

        mock_tracker.record_http.assert_awaited_once()

    async def test_captures_latency(self, hook, mock_tracker):
        resp = _make_response()

        await hook(resp)

        call_kwargs = mock_tracker.record_http.call_args.kwargs
        assert call_kwargs["latency_ms"] == pytest.approx(42.0, abs=1)

    async def test_handles_non_json_response(self, hook, mock_tracker):
        request = httpx.Request("POST", "https://shop.example.com/checkout-sessions")
        response = httpx.Response(
            status_code=500,
            request=request,
            text="Internal Server Error",
        )
        response._elapsed = timedelta(milliseconds=10)

        await hook(response)

        call_kwargs = mock_tracker.record_http.call_args.kwargs
        assert call_kwargs["response_body"] is None


class TestUCPClientEventHookPathFiltering:
    """The hook's fast-path filter must mirror the segment-aware semantics
    the middleware uses: mounted UCP base paths are accepted, segment-prefix
    lookalikes are rejected. Without this, /api/catalogue/search and
    /api/orders-history would be silently captured as UCP traffic."""

    @pytest.fixture
    def mock_tracker(self):
        tracker = MagicMock()
        tracker.record_http = AsyncMock()
        return tracker

    @pytest.fixture
    def hook(self, mock_tracker):
        return UCPClientEventHook(mock_tracker)

    async def test_captures_mounted_catalog_search(self, hook, mock_tracker):
        resp = _make_response(
            url="https://shop.example.com/ucp/v1/catalog/search",
            method="POST",
            status_code=200,
            json_body={"products": []},
        )
        await hook(resp)
        mock_tracker.record_http.assert_awaited_once()

    async def test_captures_mounted_checkout(self, hook, mock_tracker):
        resp = _make_response(
            url="https://shop.example.com/api/v2/checkout-sessions",
            method="POST",
            status_code=201,
            json_body={"id": "chk_xyz"},
        )
        await hook(resp)
        mock_tracker.record_http.assert_awaited_once()

    async def test_skips_catalogue_lookalike(self, hook, mock_tracker):
        # `/api/catalogue/search` shares a substring with `/catalog`
        # but is not a UCP path. The plain `p in path` filter would
        # have falsely captured it.
        resp = _make_response(
            url="https://shop.example.com/api/catalogue/search",
            method="POST",
            status_code=200,
            json_body={"results": []},
        )
        await hook(resp)
        mock_tracker.record_http.assert_not_awaited()

    async def test_skips_orders_history_lookalike(self, hook, mock_tracker):
        resp = _make_response(
            url="https://shop.example.com/api/orders-history",
            method="GET",
            status_code=200,
            json_body={"orders": []},
        )
        await hook(resp)
        mock_tracker.record_http.assert_not_awaited()

    async def test_skips_identity_card_lookalike(self, hook, mock_tracker):
        resp = _make_response(
            url="https://shop.example.com/api/identity-card",
            method="GET",
            status_code=200,
            json_body={"card_id": "abc"},
        )
        await hook(resp)
        mock_tracker.record_http.assert_not_awaited()

    async def test_skips_carts_preview_lookalike(self, hook, mock_tracker):
        resp = _make_response(
            url="https://shop.example.com/api/carts-preview",
            method="GET",
            status_code=200,
            json_body={"items": []},
        )
        await hook(resp)
        mock_tracker.record_http.assert_not_awaited()

    async def test_captures_oauth2_token(self, hook, mock_tracker):
        # Token endpoint — start of identity-linking analytics on the
        # client side.
        resp = _make_response(
            url="https://shop.example.com/oauth2/token",
            method="POST",
            status_code=200,
            json_body={"access_token": "redacted", "token_type": "Bearer"},
        )
        await hook(resp)
        mock_tracker.record_http.assert_awaited_once()

    async def test_captures_oauth_authorization_server_metadata(
        self, hook, mock_tracker
    ):
        # RFC 8414 metadata discovery.
        resp = _make_response(
            url="https://shop.example.com/.well-known/oauth-authorization-server",
            method="GET",
            status_code=200,
            json_body={"issuer": "https://shop.example.com"},
        )
        await hook(resp)
        mock_tracker.record_http.assert_awaited_once()

    async def test_captures_mounted_oauth2_authorize(self, hook, mock_tracker):
        resp = _make_response(
            url="https://shop.example.com/api/v1/oauth2/authorize",
            method="GET",
            status_code=302,
        )
        await hook(resp)
        mock_tracker.record_http.assert_awaited_once()

    async def test_passes_response_headers_to_tracker(self, hook, mock_tracker):
        """The hook must hand response headers off to record_http so the
        signing-header columns can land. Pin this — without it a
        merchant-signed response is invisible to analytics."""
        resp = _make_response(
            url="https://shop.example.com/checkout-sessions",
            method="POST",
            status_code=201,
            json_body={"id": "chk_123"},
            request_headers={"Signature-Input": 'sig1=();keyid="platform-K"'},
            response_headers={"signature-input": 'sig1=();keyid="merchant-K"'},
        )
        await hook(resp)
        mock_tracker.record_http.assert_awaited_once()
        call_kwargs = mock_tracker.record_http.call_args.kwargs
        # Both directions arrive at the tracker.
        assert "Signature-Input" in call_kwargs["request_headers"] or (
            "signature-input" in call_kwargs["request_headers"]
        )
        assert call_kwargs["response_headers"] is not None
        assert "signature-input" in call_kwargs["response_headers"] or (
            "Signature-Input" in call_kwargs["response_headers"]
        )

    async def test_multi_value_www_authenticate_preserved(self, hook, mock_tracker):
        """RFC 7235 §4.1 permits multiple WWW-Authenticate field lines.
        httpx.Headers ships them as separate entries; dict() flattens
        to one. The hook must coalesce them into a single comma-joined
        value so parse_bearer_challenge() can find the Bearer challenge
        even when a non-Bearer (Basic) appears on an earlier line."""
        from ucp_analytics._headers import parse_bearer_challenge

        request = httpx.Request("GET", "https://merchant.example/orders/order_123")
        response = httpx.Response(
            status_code=401,
            request=request,
            # List-of-tuples lets httpx preserve repeated headers.
            headers=[
                ("WWW-Authenticate", 'Basic realm="legacy"'),
                (
                    "WWW-Authenticate",
                    'Bearer realm="merchant", error="invalid_token"',
                ),
            ],
        )
        response._elapsed = timedelta(milliseconds=10)

        await hook(response)

        mock_tracker.record_http.assert_awaited_once()
        response_headers = mock_tracker.record_http.call_args.kwargs["response_headers"]
        # Both schemes are now reachable through the parser.
        params = parse_bearer_challenge(response_headers)
        assert params == {
            "realm": "merchant",
            "error": "invalid_token",
        }

    async def test_skips_oauth2_proxy_lookalike(self, hook, mock_tracker):
        # `/oauth2-proxy` is real infra; segment-aware filter must
        # reject it.
        resp = _make_response(
            url="https://shop.example.com/api/oauth2-proxy/start",
            method="GET",
            status_code=200,
            json_body={},
        )
        await hook(resp)
        mock_tracker.record_http.assert_not_awaited()


class TestUCPClientEventHookWebhookPathPrefixes:
    """B5b — agents that themselves receive webhooks from a partner
    platform may need the HTTPX client hook to widen capture beyond
    the default `/webhook(s)` markers. Configuration lives on the
    tracker (single source of truth, applied uniformly to capture and
    classification)."""

    @pytest.fixture
    def mock_tracker(self):
        tracker = MagicMock()
        tracker.record_http = AsyncMock()
        tracker.webhook_path_prefixes = ()
        return tracker

    async def test_default_skips_events_path(self, mock_tracker):
        """Without operator config, `/events` is not captured."""
        hook = UCPClientEventHook(mock_tracker)
        resp = _make_response(
            url="https://platform.example.com/events",
            method="POST",
            status_code=200,
            json_body={"ok": True},
        )
        await hook(resp)
        mock_tracker.record_http.assert_not_awaited()

    async def test_tracker_configured_prefix_captures_events_path(self, mock_tracker):
        mock_tracker.webhook_path_prefixes = ("/events",)
        hook = UCPClientEventHook(mock_tracker)
        resp = _make_response(
            url="https://platform.example.com/events",
            method="POST",
            status_code=200,
            json_body={"ok": True},
        )
        await hook(resp)
        mock_tracker.record_http.assert_awaited_once()

    async def test_header_pair_captures_unknown_path(self, mock_tracker):
        """Reviewer's High #1 mirror for the agent-side hook: a
        webhook delivered to the agent at an unknown URL carrying
        Standard Webhooks header pair must be captured even without
        operator-configured prefixes."""
        hook = UCPClientEventHook(mock_tracker)
        resp = _make_response(
            url="https://agent.example.com/hooks/abc",
            method="POST",
            status_code=200,
            json_body={"ok": True},
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )
        await hook(resp)
        mock_tracker.record_http.assert_awaited_once()
