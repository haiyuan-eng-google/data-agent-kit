"""Direct unit coverage for the segment-aware path-matching helper.

Lives in its own file (rather than inside test_middleware.py) so the
helper is exercised even in dev-only environments where the optional
Starlette dependency isn't installed.
"""

from __future__ import annotations

from ucp_analytics._path_match import is_webhook_delivery, path_matches_marker


class TestPathMatchesMarker:
    def test_exact_match(self):
        assert path_matches_marker("/orders", "/orders")

    def test_root_subpath(self):
        assert path_matches_marker("/orders/abc", "/orders")

    def test_mounted_exact(self):
        assert path_matches_marker("/api/v1/orders", "/orders")

    def test_mounted_subpath(self):
        assert path_matches_marker("/api/v1/orders/abc", "/orders")

    def test_segment_prefix_lookalike_rejected(self):
        # Word-bounded — `/orders` must not match `/reorders`,
        # `/orders-history`, `/orders-archive/...`.
        assert not path_matches_marker("/api/v1/reorders", "/orders")
        assert not path_matches_marker("/api/v1/orders-history", "/orders")
        assert not path_matches_marker("/api/v1/orders-archive/abc", "/orders")

    def test_segment_in_middle_lookalike_rejected(self):
        # `/orders` must be word-bounded by `/`, not concatenated.
        assert not path_matches_marker("/myorders/abc", "/orders")

    def test_dotted_well_known_path(self):
        # Markers that themselves contain `.` (e.g. /.well-known/ucp)
        # behave the same way under the helper.
        assert path_matches_marker("/.well-known/ucp", "/.well-known/ucp")
        assert path_matches_marker("/api/.well-known/ucp", "/.well-known/ucp")
        assert not path_matches_marker(
            "/api/.well-known/ucp-something", "/.well-known/ucp"
        )

    def test_catalog_does_not_match_catalogue(self):
        assert not path_matches_marker("/api/catalogue/search", "/catalog")


class TestIsWebhookDelivery:
    """B5b — UCP `order.md` says webhook URL format is
    platform-specific. The helper accepts either a default/configured
    path prefix OR the Standard Webhooks header pair (Webhook-Id +
    Webhook-Timestamp). Header-based detection is suppressed on known
    non-webhook UCP REST paths so stamped headers can't override the
    URL there."""

    def test_default_webhooks_prefix(self):
        assert is_webhook_delivery("/webhooks/partners/p1/events/order")
        assert is_webhook_delivery("/webhook/incoming")

    def test_default_webhook_prefix_mounted(self):
        # Real platforms mount under /ucp/v1, /api/v2, etc. Segment
        # match handles the mount.
        assert is_webhook_delivery("/api/v1/webhooks/orders")

    def test_unknown_path_no_headers_returns_false(self):
        assert not is_webhook_delivery("/events")
        assert not is_webhook_delivery("/hooks/abc")

    def test_extra_prefix_widens_detection(self):
        # Operator-configured webhook prefix.
        assert is_webhook_delivery("/events", extra_prefixes=("/events",))
        assert is_webhook_delivery("/ucp-events/x", extra_prefixes=("/ucp-events",))

    def test_header_pair_alone_triggers_on_unknown_path(self):
        # The "/events" path isn't in the default prefixes, but the
        # Standard Webhooks header pair is present.
        assert is_webhook_delivery(
            "/events",
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )

    def test_header_pair_case_insensitive(self):
        # HTTP headers are case-insensitive per RFC 7230. Standard
        # Webhooks ships them with "Webhook-Id" but real middleware
        # may normalize to lowercase or PascalCase.
        assert is_webhook_delivery(
            "/hooks/x",
            request_headers={"webhook-id": "1", "webhook-timestamp": "1"},
        )
        assert is_webhook_delivery(
            "/hooks/x",
            request_headers={"WEBHOOK-ID": "1", "WEBHOOK-TIMESTAMP": "1"},
        )

    def test_only_one_header_does_not_trigger(self):
        # The pair must be complete — one alone is not a Standard
        # Webhooks delivery.
        assert not is_webhook_delivery(
            "/events", request_headers={"Webhook-Id": "evt_42"}
        )
        assert not is_webhook_delivery(
            "/events", request_headers={"Webhook-Timestamp": "1767225600"}
        )

    def test_header_fallback_suppressed_on_checkout_path(self):
        # Webhook headers stamped on /checkout-sessions are spurious;
        # the URL is authoritative for known REST endpoints.
        assert not is_webhook_delivery(
            "/checkout-sessions",
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )

    def test_header_fallback_suppressed_on_carts_path(self):
        assert not is_webhook_delivery(
            "/api/v1/carts",
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )

    def test_header_fallback_suppressed_on_orders_rest_path(self):
        # /orders is the REST endpoint, not the webhook — headers
        # don't override.
        assert not is_webhook_delivery(
            "/orders/order_abc",
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )

    def test_header_fallback_suppressed_on_oauth_path(self):
        assert not is_webhook_delivery(
            "/oauth2/token",
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )

    def test_header_fallback_suppressed_on_well_known(self):
        assert not is_webhook_delivery(
            "/.well-known/ucp",
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )

    def test_no_headers_no_match_returns_false(self):
        assert not is_webhook_delivery("/api/random", request_headers=None)
        assert not is_webhook_delivery("/api/random", request_headers={})

    def test_handles_non_string_header_keys(self):
        # Some test/mocked headers dicts may carry non-string keys;
        # the helper must skip them rather than crash.
        assert not is_webhook_delivery("/events", request_headers={42: "value"})
