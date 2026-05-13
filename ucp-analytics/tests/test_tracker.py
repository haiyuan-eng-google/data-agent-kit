"""Tests for UCPAnalyticsTracker."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from ucp_analytics.tracker import UCPAnalyticsTracker


@pytest.fixture
def mock_writer():
    with patch("ucp_analytics.tracker.AsyncBigQueryWriter") as MockWriter:
        instance = MockWriter.return_value
        instance.enqueue = AsyncMock()
        instance.flush = AsyncMock()
        instance.close = AsyncMock()
        yield instance


@pytest.fixture
def tracker(mock_writer):
    return UCPAnalyticsTracker(project_id="test-project", app_name="test_app")


class TestRecordHttp:
    async def test_basic_record(self, tracker, mock_writer):
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/checkout-sessions",
            status_code=201,
            response_body={"id": "chk_123", "status": "incomplete"},
        )

        assert event.event_type == "checkout_session_created"
        assert event.merchant_host == "merchant.example.com"
        assert event.http_method == "POST"
        assert event.checkout_session_id == "chk_123"
        mock_writer.enqueue.assert_awaited_once()

    async def test_path_from_url(self, tracker, mock_writer):
        event = await tracker.record_http(
            method="GET",
            url="https://shop.example.com/.well-known/ucp",
            status_code=200,
        )

        assert event.event_type == "profile_discovered"
        assert event.http_path == "/.well-known/ucp"

    async def test_explicit_path_overrides_url(self, tracker, mock_writer):
        event = await tracker.record_http(
            method="GET",
            url="https://shop.example.com/other",
            path="/.well-known/ucp",
            status_code=200,
        )

        assert event.event_type == "profile_discovered"

    async def test_latency_recorded(self, tracker, mock_writer):
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            latency_ms=42.5,
        )

        assert event.latency_ms == 42.5

    async def test_custom_metadata_attached(self, mock_writer):
        tracker = UCPAnalyticsTracker(
            project_id="test",
            custom_metadata={"env": "prod", "region": "us-west"},
        )
        event = await tracker.record_http(
            method="GET",
            path="/.well-known/ucp",
            status_code=200,
        )

        meta = json.loads(event.custom_metadata_json)
        assert meta["env"] == "prod"
        assert meta["region"] == "us-west"

    async def test_headers_extracted(self, tracker, mock_writer):
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "ucp-agent": 'profile="https://agent.example.com"',
                "idempotency-key": "idem_123",
                "request-id": "req_456",
            },
        )

        assert "agent.example.com" in event.platform_profile_url
        assert event.idempotency_key == "idem_123"
        assert event.request_id == "req_456"

    async def test_webhook_uses_request_body(self, tracker, mock_writer):
        """Webhook: order payload in request_body, response is ack."""
        order_payload = {
            "id": "order_xyz",
            "checkout_id": "chk_abc",
            "status": "shipped",
        }
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/webhooks/partners/p1/events/order",
            status_code=200,
            request_body=order_payload,
            response_body={"status": "ok"},
        )

        assert event.event_type == "order_shipped"
        assert event.order_id == "order_xyz"
        assert event.checkout_session_id == "chk_abc"

    async def test_webhook_falls_back_to_response_body_when_no_request(
        self, tracker, mock_writer
    ):
        """Webhook callers that only have the response side in hand must
        still produce a populated row. Pin this fallback so the order
        payload-extraction doesn't regress when the new request/response
        merge logic short-circuits webhook flows."""
        order_payload = {
            "id": "order_123",
            "checkout_id": "chk_123",
            "status": "delivered",
        }
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/webhooks/partners/p1/events/order",
            status_code=200,
            request_body=None,
            response_body=order_payload,
        )

        assert event.event_type == "order_delivered"
        assert event.order_id == "order_123"
        assert event.checkout_session_id == "chk_123"

    async def test_singular_webhook_uses_request_body(self, tracker, mock_writer):
        """Legacy /webhook/ (singular) should also use request_body."""
        order_payload = {
            "id": "order_abc",
            "checkout_id": "chk_xyz",
            "status": "delivered",
        }
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/webhook/order-delivered",
            status_code=200,
            request_body=order_payload,
            response_body={"status": "ok"},
        )

        assert event.order_id == "order_abc"
        assert event.checkout_session_id == "chk_xyz"

    async def test_request_body_context_survives_response_body(
        self, tracker, mock_writer
    ):
        """A checkout-create exchange has Context only on the request side
        (the platform tells the merchant the buyer's intent / locale /
        currency); the response carries the resolved checkout state.
        Both must end up on the same row."""
        request_body = {
            "context": {
                "intent": "buy a birthday gift",
                "language": "en-US",
                "currency": "USD",
                "eligibility": ["dev.example.loyalty_member"],
            },
            "line_items": [{"item": {"id": "sku_rose"}, "quantity": 1}],
        }
        response_body = {
            "id": "chk_123",
            "status": "ready_for_complete",
            "currency": "USD",
        }
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/checkout-sessions",
            status_code=201,
            request_body=request_body,
            response_body=response_body,
        )

        # Response-only fields land.
        assert event.event_type == "checkout_session_created"
        assert event.checkout_session_id == "chk_123"
        assert event.checkout_status == "ready_for_complete"
        assert event.currency == "USD"
        # Request-only fields survive the response-side extraction.
        assert event.context_intent == "buy a birthday gift"
        assert event.context_language == "en-US"
        assert event.context_currency == "USD"
        assert event.context_eligibility_json is not None
        assert "dev.example.loyalty_member" in event.context_eligibility_json

    # --- HTTP message signing (RFC 9421 / UCP signatures.md) ---

    async def test_unsigned_exchange_marks_both_directions_false(
        self, tracker, mock_writer
    ):
        """An exchange with observed-but-unsigned headers records
        request_signed=False / response_signed=False — distinct from a
        row where headers were never observed at all."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={"content-type": "application/json"},
            response_headers={"content-type": "application/json"},
        )
        assert event.request_signed is False
        assert event.response_signed is False
        assert event.request_signature_keyid is None
        assert event.response_signature_keyid is None

    async def test_unobserved_headers_record_none_not_false(self, tracker, mock_writer):
        """Direct callers that don't pass headers at all must record
        request_signed / response_signed as None (unknown), not False
        (observed unsigned). Without this the "% signed traffic" KPI
        is biased downward by every direct-API row."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            # request_headers and response_headers both omitted entirely
        )
        assert event.request_signed is None
        assert event.response_signed is None
        assert event.request_signature_keyid is None
        assert event.response_signature_keyid is None

    async def test_request_side_signing_extracts_keyid(self, tracker, mock_writer):
        """Request signed by the platform: extract request_signed=True and
        the keyid for joining against /.well-known/ucp signing_keys[].
        Both Signature-Input AND Signature must be present together
        per UCP signatures.md."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "Signature-Input": (
                    'sig1=("@method" "@path" "host");'
                    'keyid="platform-key-1";created=1770000000'
                ),
                "Signature": "sig1=:abc==:",
            },
            response_headers={"content-type": "application/json"},
        )
        assert event.request_signed is True
        assert event.request_signature_keyid == "platform-key-1"
        assert event.response_signed is False
        assert event.response_signature_keyid is None

    async def test_half_signed_request_is_not_counted_as_signed(
        self, tracker, mock_writer
    ):
        """Signature-Input present but Signature missing → malformed
        half-signature. Must record request_signed=False so the KPI
        doesn't inflate on incomplete senders. We still parse the keyid
        for forensic purposes — useful when debugging which platforms
        are sending half-signed traffic."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                # Metadata header only, no actual signature value.
                "Signature-Input": 'sig1=();keyid="platform-key-1"',
            },
            response_headers={"content-type": "application/json"},
        )
        assert event.request_signed is False
        assert event.request_signature_keyid == "platform-key-1"

    async def test_response_side_signing_extracts_keyid(self, tracker, mock_writer):
        """Response signed by the merchant: extract response_signed=True
        independently from request side, since request and response can
        be signed by different parties."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={"content-type": "application/json"},
            response_headers={
                "signature-input": 'sig1=();keyid="merchant-key-A"',
                "signature": "sig1=:def==:",
            },
        )
        assert event.request_signed is False
        assert event.response_signed is True
        assert event.response_signature_keyid == "merchant-key-A"
        assert event.request_signature_keyid is None

    async def test_both_sides_signed_with_distinct_keyids(self, tracker, mock_writer):
        """Platform-signed request, merchant-signed response. Distinct
        keyids land in their respective columns — pinned because
        conflating them was an explicit issue #8 acceptance concern."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "Signature-Input": 'sig1=();keyid="platform-K"',
                "Signature": "sig1=:abc==:",
            },
            response_headers={
                "Signature-Input": 'sig1=();keyid="merchant-K"',
                "Signature": "sig1=:def==:",
            },
        )
        assert event.request_signed is True
        assert event.response_signed is True
        assert event.request_signature_keyid == "platform-K"
        assert event.response_signature_keyid == "merchant-K"

    async def test_signing_lookup_is_case_insensitive(self, tracker, mock_writer):
        """Real middleware hands the headers off in either casing
        depending on the framework. Pin case-insensitive lookup."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "SIGNATURE-INPUT": 'sig1=();keyid="upper-K"',
                "SIGNATURE": "sig1=:abc==:",
            },
            response_headers={
                "sIgNaTuRe-InPuT": 'sig1=();keyid="mixed-K"',
                "sIgNaTuRe": "sig1=:def==:",
            },
        )
        assert event.request_signed is True
        assert event.response_signed is True
        assert event.request_signature_keyid == "upper-K"
        assert event.response_signature_keyid == "mixed-K"

    # ---- C5c: signature algorithm per direction via jwk_lookup ----

    async def test_no_jwk_lookup_leaves_alg_columns_none(self, tracker, mock_writer):
        """Default tracker has no jwk_lookup; the alg columns stay
        None even when a fully signed exchange is recorded. Pinned so
        the new column doesn't accidentally infer alg from anything
        other than the JWK lookup."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "signature-input": 'sig1=();keyid="merchant-K"',
                "signature": "sig1=:abc==:",
            },
            response_headers={
                "signature-input": 'sig1=();keyid="platform-K"',
                "signature": "sig1=:def==:",
            },
        )
        assert event.request_signature_keyid == "merchant-K"
        assert event.response_signature_keyid == "platform-K"
        # No lookup → no alg.
        assert event.request_signature_alg is None
        assert event.response_signature_alg is None

    async def test_request_side_alg_derived_from_jwk_lookup(self, mock_writer):
        """Per-direction derivation: the request-side keyid resolves
        to a P-256 JWK → ES256 populates request_signature_alg."""
        jwks = {"merchant-K": {"kty": "EC", "crv": "P-256"}}
        tracker = UCPAnalyticsTracker(
            project_id="test",
            app_name="test_app",
            jwk_lookup=jwks.get,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "signature-input": 'sig1=();keyid="merchant-K"',
                "signature": "sig1=:abc==:",
            },
        )
        assert event.request_signature_alg == "ES256"
        # No response-side signature → response alg stays None.
        assert event.response_signature_alg is None

    async def test_response_side_alg_derived_independently(self, mock_writer):
        """Request and response can be signed by different parties
        with different curves. Pin that a P-256 request and a P-384
        response each derive their own alg without crosstalk."""
        jwks = {
            "merchant-K": {"kty": "EC", "crv": "P-256"},
            "platform-K": {"kty": "EC", "crv": "P-384"},
        }
        tracker = UCPAnalyticsTracker(
            project_id="test",
            app_name="test_app",
            jwk_lookup=jwks.get,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "signature-input": 'sig1=();keyid="merchant-K"',
                "signature": "sig1=:abc==:",
            },
            response_headers={
                "signature-input": 'sig1=();keyid="platform-K"',
                "signature": "sig1=:def==:",
            },
        )
        assert event.request_signature_alg == "ES256"
        assert event.response_signature_alg == "ES384"

    async def test_unknown_keyid_lookup_misses_alg_stays_none(self, mock_writer):
        """jwk_lookup returns None for an unknown keyid → alg column
        stays None. Distinct from "no lookup configured" — both end
        up NULL on the row, but the operator can tell from the
        application's JWKS source which case applies."""
        jwks = {"known-K": {"kty": "EC", "crv": "P-256"}}
        tracker = UCPAnalyticsTracker(
            project_id="test",
            app_name="test_app",
            jwk_lookup=jwks.get,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "signature-input": 'sig1=();keyid="unknown-K"',
                "signature": "sig1=:abc==:",
            },
        )
        assert event.request_signature_keyid == "unknown-K"
        assert event.request_signature_alg is None

    async def test_unknown_curve_alg_stays_none(self, mock_writer):
        """JWK is found but its curve doesn't map to any JWA alg
        (and no `alg` fallback on the JWK itself) → column stays
        None. Pinned so a future P-521 deployment doesn't silently
        record the wrong alg via some default."""
        jwks = {"merchant-K": {"kty": "EC", "crv": "P-521"}}
        tracker = UCPAnalyticsTracker(
            project_id="test",
            app_name="test_app",
            jwk_lookup=jwks.get,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "signature-input": 'sig1=();keyid="merchant-K"',
                "signature": "sig1=:abc==:",
            },
        )
        assert event.request_signature_keyid == "merchant-K"
        assert event.request_signature_alg is None

    async def test_no_signature_headers_no_alg_lookup(self, mock_writer):
        """Plain unsigned exchange — no keyid to resolve, so we never
        call jwk_lookup. Pin no-call so a buggy JWKS source can't be
        invoked on traffic that isn't signed in the first place."""
        from unittest.mock import MagicMock

        jwk_lookup = MagicMock(return_value=None)
        tracker = UCPAnalyticsTracker(
            project_id="test",
            app_name="test_app",
            jwk_lookup=jwk_lookup,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={},
            response_headers={},
        )
        jwk_lookup.assert_not_called()
        assert event.request_signature_alg is None
        assert event.response_signature_alg is None

    async def test_half_signed_request_does_not_record_alg(self, mock_writer):
        """#12 intentionally extracts the keyid even from a half-signed
        exchange (Signature-Input without Signature) for forensics.
        That row records `request_signed=False` with a populated
        `request_signature_keyid`. The alg column MUST stay None on
        such rows -- recording an alg there would make the row look
        like signed crypto-agility data on an unsigned exchange.

        Pin the gating so the alg column is only populated when the
        full Signature pair is present (`request_signed=True`)."""
        jwks = {"merchant-K": {"kty": "EC", "crv": "P-256"}}
        tracker = UCPAnalyticsTracker(
            project_id="test",
            app_name="test_app",
            jwk_lookup=jwks.get,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                # Half-signed: Signature-Input present, Signature missing.
                "signature-input": 'sig1=();keyid="merchant-K"',
            },
        )
        # #12 still captures the keyid for forensics.
        assert event.request_signature_keyid == "merchant-K"
        # But the row isn't actually signed.
        assert event.request_signed is False
        # And alg must stay None -- no crypto-agility signal on
        # incomplete signature pairs.
        assert event.request_signature_alg is None

    async def test_half_signed_response_does_not_record_alg(self, mock_writer):
        """Mirror of the half-signed request test on the response side."""
        jwks = {"platform-K": {"kty": "EC", "crv": "P-384"}}
        tracker = UCPAnalyticsTracker(
            project_id="test",
            app_name="test_app",
            jwk_lookup=jwks.get,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_headers={
                # Signature without Signature-Input metadata.
                "signature": "sig1=:abc==:",
                # Intentionally NO signature-input, so response_signed=False.
            },
        )
        assert event.response_signed is False
        # No keyid either (it lives in Signature-Input).
        assert event.response_signature_keyid is None
        assert event.response_signature_alg is None

    # ---- A3: embedded checkout `ec_color_scheme` URL query param ----

    async def test_ec_color_scheme_extracted_from_request_url(
        self, tracker, mock_writer
    ):
        """The host appends `?ec_color_scheme=dark` to the embedded
        checkout URL to request a theme. Pin that the column captures
        the value as sent."""
        event = await tracker.record_http(
            method="GET",
            url="https://merchant.example.com/embedded-checkout?ec_color_scheme=dark",
            status_code=200,
        )
        assert event.embedded_ec_color_scheme == "dark"

    async def test_ec_color_scheme_takes_first_value_when_repeated(
        self, tracker, mock_writer
    ):
        """URLs can repeat a query key; we take the first value to
        mirror how a web server would surface `request.args[k]`.
        Senders that ship duplicates have a bug, but analytics
        records what was *actually* requested (the first param a
        right-most-wins parser would skip)."""
        event = await tracker.record_http(
            method="GET",
            url=(
                "https://merchant.example.com/embedded-checkout"
                "?ec_color_scheme=light&ec_color_scheme=dark"
            ),
            status_code=200,
        )
        assert event.embedded_ec_color_scheme == "light"

    async def test_ec_color_scheme_absent_leaves_column_none(
        self, tracker, mock_writer
    ):
        """No `ec_color_scheme` param on the URL → column stays None.
        The signal is "did the host request a theme", so absence is
        meaningful."""
        event = await tracker.record_http(
            method="GET",
            url="https://merchant.example.com/embedded-checkout",
            status_code=200,
        )
        assert event.embedded_ec_color_scheme is None

    async def test_ec_color_scheme_non_spec_value_recorded_as_sent(
        self, tracker, mock_writer
    ):
        """Spec lists `light` / `dark`, but the param is host-
        controlled. We record signal fidelity — non-spec values land
        in the column rather than being normalized away, so analysts
        can detect senders that ship typos / experimental themes."""
        event = await tracker.record_http(
            method="GET",
            url=(
                "https://merchant.example.com/embedded-checkout"
                "?ec_color_scheme=high-contrast"
            ),
            status_code=200,
        )
        assert event.embedded_ec_color_scheme == "high-contrast"

    async def test_ec_color_scheme_coexists_with_other_query_params(
        self, tracker, mock_writer
    ):
        """Real embedded URLs carry several params (`cart_id`,
        `session_id`, etc.). Pin that other params don't interfere
        and `ec_color_scheme` is picked specifically — not just
        the first param on the URL."""
        event = await tracker.record_http(
            method="GET",
            url=(
                "https://merchant.example.com/embedded-checkout"
                "?cart_id=cart_abc&ec_color_scheme=dark&session_id=sess_123"
            ),
            status_code=200,
        )
        assert event.embedded_ec_color_scheme == "dark"

    async def test_ec_color_scheme_extracted_from_path_when_url_absent(
        self, tracker, mock_writer
    ):
        """record_http accepts both `url` and `path`; direct callers
        often pass only `path` with a query string on it. The
        extraction must read the query from whichever source carries
        one — without this fallback the signal silently drops on
        path-only callers. Reviewer's PR-22 repro."""
        # Path-only, query string on path → captured.
        event = await tracker.record_http(
            method="GET",
            path="/embedded-checkout?ec_color_scheme=dark",
            status_code=200,
        )
        assert event.embedded_ec_color_scheme == "dark"

    async def test_ec_color_scheme_none_when_path_has_no_query(
        self, tracker, mock_writer
    ):
        """Path-only without a query string → column stays None.
        Pins the negative side so the fallback path doesn't
        accidentally invent a value."""
        event = await tracker.record_http(
            method="GET",
            path="/embedded-checkout",
            status_code=200,
        )
        assert event.embedded_ec_color_scheme is None

    async def test_ec_color_scheme_url_wins_when_both_carry_query(
        self, tracker, mock_writer
    ):
        """When both `url` and `path` are supplied and both carry a
        query string, the `url`'s query wins. This matches the
        existing precedence elsewhere in record_http where url-
        derived values are authoritative — the caller's url is the
        fully-qualified original, path is the deployment-resolved
        derivative."""
        event = await tracker.record_http(
            method="GET",
            url="https://merchant.example.com/checkout?ec_color_scheme=light",
            path="/checkout?ec_color_scheme=dark",
            status_code=200,
        )
        assert event.embedded_ec_color_scheme == "light"

    async def test_jwk_lookup_exception_swallowed(self, mock_writer):
        """A flaky JWKS source (network error, cache miss, etc.) must
        not take down the analytics row. Exception is caught, logged,
        and the alg column stays None."""

        def broken_lookup(keyid):
            raise RuntimeError("JWKS server down")

        tracker = UCPAnalyticsTracker(
            project_id="test",
            app_name="test_app",
            jwk_lookup=broken_lookup,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "signature-input": 'sig1=();keyid="merchant-K"',
                "signature": "sig1=:abc==:",
            },
        )
        # The row still records — keyid captured, alg stays None.
        assert event.request_signature_keyid == "merchant-K"
        assert event.request_signature_alg is None

    # --- Standard Webhooks metadata (UCP order.md) ---

    async def test_webhook_headers_extracted_from_request_side(
        self, tracker, mock_writer
    ):
        """Inbound webhooks carry Webhook-Id and Webhook-Timestamp on
        the request. Both columns must land — these are the join keys
        for de-duping deliveries and correlating to merchant outbound
        events."""
        event = await tracker.record_http(
            method="POST",
            url="https://platform.example.com/webhooks/partners/p1/events/order",
            status_code=200,
            request_body={
                "id": "order_abc",
                "checkout_id": "chk_xyz",
                "status": "delivered",
            },
            request_headers={
                "Webhook-Id": "evt_2026_05_08_001",
                "Webhook-Timestamp": "1767225600",  # 2026-01-01T00:00:00Z
            },
        )
        assert event.webhook_id == "evt_2026_05_08_001"
        assert event.webhook_timestamp == "2026-01-01T00:00:00+00:00"
        # Existing webhook extraction still works.
        assert event.event_type == "order_delivered"
        assert event.order_id == "order_abc"

    async def test_webhook_timestamp_garbage_does_not_crash_row(
        self, tracker, mock_writer
    ):
        """A malformed Webhook-Timestamp from one bad sender must not
        take down the whole row — webhook_timestamp lands as None and
        the rest of the event still records."""
        event = await tracker.record_http(
            method="POST",
            url="https://platform.example.com/webhooks/partners/p1/events/order",
            status_code=200,
            request_headers={
                "Webhook-Id": "evt_xyz",
                "Webhook-Timestamp": "not-a-unix-timestamp",
            },
            request_body={
                "id": "order_xyz",
                "checkout_id": "chk_xyz",
                "status": "delivered",
            },
        )
        assert event.webhook_id == "evt_xyz"
        assert event.webhook_timestamp is None
        assert event.event_type == "order_delivered"

    async def test_non_webhook_traffic_has_no_webhook_metadata(
        self, tracker, mock_writer
    ):
        """Regular checkout traffic doesn't carry Webhook-* headers,
        so the columns stay None — distinct from a webhook delivery."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={"content-type": "application/json"},
            response_headers={"content-type": "application/json"},
        )
        assert event.webhook_id is None
        assert event.webhook_timestamp is None

    async def test_ucp_agent_profile_url_parsed_from_header(self, tracker, mock_writer):
        """UCP-Agent: profile=\"...\" is an RFC 8941 Dictionary; we need
        the parsed URI in `ucp_agent_profile_url` for clean joins, not
        the raw structured-field string. The legacy
        `platform_profile_url` field still carries the raw value for
        backwards compatibility."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={
                "ucp-agent": 'profile="https://platform.example/profile"',
            },
        )
        assert event.ucp_agent_profile_url == "https://platform.example/profile"
        # Backwards compat: legacy column still populated.
        assert "platform.example" in event.platform_profile_url

    async def test_ucp_agent_profile_url_works_on_webhook(self, tracker, mock_writer):
        """On business → platform webhooks the UCP-Agent value carries
        the *business's* profile, not the platform's. The neutral
        `ucp_agent_profile_url` column captures both directions
        cleanly; pinned because the legacy field name is misleading
        in this direction."""
        event = await tracker.record_http(
            method="POST",
            url="https://platform.example/webhooks/partners/p1/events/order",
            status_code=200,
            request_headers={
                "UCP-Agent": 'profile="https://merchant.example/profile"',
                "Webhook-Id": "evt_1",
                "Webhook-Timestamp": "1767225600",
            },
            request_body={
                "id": "order_a",
                "checkout_id": "chk_a",
                "status": "delivered",
            },
        )
        assert event.ucp_agent_profile_url == "https://merchant.example/profile"

    async def test_ucp_agent_profile_url_absent_records_none(
        self, tracker, mock_writer
    ):
        """Traffic without UCP-Agent records None — distinct from a
        sender that included the header but didn't include a profile
        member."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={"content-type": "application/json"},
        )
        assert event.ucp_agent_profile_url is None

    async def test_ucp_agent_profile_url_malformed_records_none(
        self, tracker, mock_writer
    ):
        """Header present but missing the `profile` member → None;
        legacy column still carries the raw header for forensics."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_headers={"ucp-agent": 'version="2026-04-08"'},
        )
        assert event.ucp_agent_profile_url is None
        assert event.platform_profile_url == 'version="2026-04-08"'

    # --- WWW-Authenticate Bearer challenge (RFC 7235 / 6750) ---

    async def test_auth_challenge_extracted_from_response_headers(
        self, tracker, mock_writer
    ):
        """A 401 with a full Bearer challenge populates all four
        auth_challenge_* columns, parsed off the response side."""
        challenge = (
            'Bearer realm="https://merchant.example",'
            ' error="insufficient_scope",'
            ' scope="dev.ucp.shopping.order:manage",'
            " resource_metadata="
            '"https://merchant.example/.well-known/oauth-protected-resource"'
        )
        event = await tracker.record_http(
            method="GET",
            path="/orders/order_123",
            status_code=401,
            response_headers={"WWW-Authenticate": challenge},
        )
        assert event.auth_challenge_realm == "https://merchant.example"
        assert event.auth_challenge_error == "insufficient_scope"
        assert event.auth_challenge_scope == "dev.ucp.shopping.order:manage"
        assert (
            event.auth_challenge_resource_metadata
            == "https://merchant.example/.well-known/oauth-protected-resource"
        )

    async def test_auth_challenge_invalid_token(self, tracker, mock_writer):
        """invalid_token challenges populate error + realm only."""
        event = await tracker.record_http(
            method="GET",
            path="/orders/order_123",
            status_code=401,
            response_headers={
                "WWW-Authenticate": (
                    'Bearer realm="https://merchant.example", error="invalid_token"'
                )
            },
        )
        assert event.auth_challenge_realm == "https://merchant.example"
        assert event.auth_challenge_error == "invalid_token"
        assert event.auth_challenge_scope is None
        assert event.auth_challenge_resource_metadata is None

    async def test_no_auth_challenge_when_header_absent(self, tracker, mock_writer):
        """Successful exchanges don't carry a Bearer challenge — all
        four columns stay None."""
        event = await tracker.record_http(
            method="GET",
            path="/orders/order_123",
            status_code=200,
            response_headers={"content-type": "application/json"},
        )
        assert event.auth_challenge_realm is None
        assert event.auth_challenge_error is None
        assert event.auth_challenge_scope is None
        assert event.auth_challenge_resource_metadata is None

    async def test_no_auth_challenge_when_response_headers_omitted(
        self, tracker, mock_writer
    ):
        """Direct callers that don't pass response_headers at all get
        None for the auth_challenge_* columns."""
        event = await tracker.record_http(
            method="GET",
            path="/orders/order_123",
            status_code=401,
        )
        assert event.auth_challenge_realm is None
        assert event.auth_challenge_error is None
        assert event.auth_challenge_scope is None
        assert event.auth_challenge_resource_metadata is None

    async def test_basic_challenge_does_not_populate_columns(
        self, tracker, mock_writer
    ):
        """A non-Bearer scheme (e.g. Basic) doesn't trip the helper —
        we only care about Bearer for UCP auth analytics."""
        event = await tracker.record_http(
            method="GET",
            path="/orders/order_123",
            status_code=401,
            response_headers={
                "WWW-Authenticate": 'Basic realm="merchant"',
            },
        )
        assert event.auth_challenge_realm is None
        assert event.auth_challenge_error is None

    async def test_auth_challenge_lookup_is_case_insensitive(
        self, tracker, mock_writer
    ):
        """The helper itself is case-insensitive on the header name and
        scheme — pin that the tracker doesn't pre-lowercase or normalize
        in a way that would break the parser."""
        event = await tracker.record_http(
            method="GET",
            path="/orders/order_123",
            status_code=401,
            response_headers={
                "www-authenticate": ('BEARER realm="x", error="insufficient_scope"')
            },
        )
        assert event.auth_challenge_realm == "x"
        assert event.auth_challenge_error == "insufficient_scope"

    # ---- A5: eligibility verification outcome end-to-end ----

    async def test_eligibility_accepted_flows_through_record_http(
        self, tracker, mock_writer
    ):
        """End-to-end: a checkout response shipping
        `eligibility_accepted` as an info-severity message populates
        the trio on the UCPEvent. This pins the parser→event setattr
        plumbing for the new fields, not just the parser unit."""
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "status": "ready_for_complete",
                "messages": [
                    {
                        "type": "info",
                        "code": "eligibility_accepted",
                        "content": "Loyalty member discount applies",
                    },
                ],
            },
        )
        assert event.eligibility_accepted_present is True
        assert event.eligibility_not_accepted_present is False
        assert event.eligibility_invalid_present is False

    async def test_eligibility_invalid_flows_through_from_error_severity(
        self, tracker, mock_writer
    ):
        """`eligibility_invalid` is canonically an error-severity code.
        The cross-severity walk in the parser must populate the trio
        from an error message, not just from info messages."""
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/checkout-sessions",
            status_code=400,
            response_body={
                "messages": [
                    {
                        "type": "error",
                        "code": "eligibility_invalid",
                        "content": "Eligibility claim malformed",
                    },
                ],
            },
        )
        assert event.eligibility_invalid_present is True
        assert event.eligibility_accepted_present is False
        assert event.eligibility_not_accepted_present is False
        # Legacy first-error column remains populated from the same msg.
        assert event.error_code == "eligibility_invalid"

    async def test_no_eligibility_codes_leaves_trio_none(self, tracker, mock_writer):
        """A checkout with no eligibility outcome code leaves all
        three columns None — they're three-state nullable BOOLs and
        NULL is the explicit 'verification did not surface' signal."""
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "status": "ready_for_complete",
                "messages": [
                    {"type": "info", "code": "tax_rounded_up"},
                ],
            },
        )
        assert event.eligibility_accepted_present is None
        assert event.eligibility_not_accepted_present is None
        assert event.eligibility_invalid_present is None

    async def test_webhook_headers_rejected_on_non_webhook_path(
        self, tracker, mock_writer
    ):
        """Per UCP order.md, Webhook-Id / Webhook-Timestamp belong to
        the Order Event Webhook flow only. A buggy or malicious sender
        could stamp these headers onto a regular checkout / cart /
        catalog request; we must NOT capture them there — webhook
        metadata on a checkout row would corrupt de-dup / lag /
        correlation queries."""
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/checkout-sessions",
            status_code=201,
            request_headers={
                "Webhook-Id": "evt_definitely_not_a_webhook",
                "Webhook-Timestamp": "1767225600",
            },
            response_body={"id": "chk_xyz", "status": "ready_for_complete"},
        )
        # Path-scoped: not a webhook, so headers are dropped.
        assert event.event_type == "checkout_session_created"
        assert event.webhook_id is None
        assert event.webhook_timestamp is None

    async def test_webhook_lookup_is_case_insensitive(self, tracker, mock_writer):
        event = await tracker.record_http(
            method="POST",
            url="https://platform.example.com/webhooks/partners/p1/events/order",
            status_code=200,
            request_headers={
                "WEBHOOK-ID": "evt_upper",
                "wEbHoOk-TiMeStAmP": "1767225600",
            },
            request_body={
                "id": "order_a",
                "checkout_id": "chk_a",
                "status": "delivered",
            },
        )
        assert event.webhook_id == "evt_upper"
        assert event.webhook_timestamp == "2026-01-01T00:00:00+00:00"

    # ---- B5b: platform-provided webhook URLs + header fallback ----

    async def test_webhook_headers_capture_on_unknown_path(self, tracker, mock_writer):
        """A platform that publishes its webhook URL as `/hooks/<id>`
        (not `/webhook(s)/...`) still flows through analytics correctly:
        webhook_id / webhook_timestamp populate, classification routes
        through ORDER_*, and is_webhook gating accepts the row."""
        event = await tracker.record_http(
            method="POST",
            url="https://platform.example.com/hooks/abc-123",
            status_code=200,
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
            request_body={
                "id": "order_xyz",
                "checkout_id": "chk_a",
                "status": "shipped",
            },
        )
        # webhook_id captured even though path is non-default.
        assert event.webhook_id == "evt_42"
        assert event.webhook_timestamp == "2026-01-01T00:00:00+00:00"
        # Classification routed via the body status.
        assert event.event_type == "order_shipped"

    async def test_configured_webhook_path_prefix(self, mock_writer):
        """An operator can pass webhook_path_prefixes at construction
        so a platform that publishes `/events` as its webhook URL gets
        captured without the operator having to rely on the header
        fallback alone (helpful when senders batch deliveries without
        per-request Webhook-Id)."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            app_name="test_app",
            webhook_path_prefixes=["/events", "/ucp-events"],
        )
        event = await tracker.record_http(
            method="POST",
            url="https://platform.example.com/events",
            status_code=200,
            request_body={
                "id": "order_xyz",
                "checkout_id": "chk_a",
                "status": "delivered",
            },
        )
        assert event.event_type == "order_delivered"
        # is_webhook fires from the configured prefix → request body
        # used for extraction (not response). order_id is derived
        # because the body shape (id + checkout_id) is order-shaped.
        assert event.order_id == "order_xyz"

    async def test_webhook_headers_on_non_webhook_path_still_rejected(
        self, tracker, mock_writer
    ):
        """B5b adds header-based webhook detection but MUST NOT weaken
        the existing protection: webhook headers stamped on
        /checkout-sessions still record nothing. The URL is
        authoritative for known UCP REST endpoints; the header pair
        only triggers fallback on unknown paths."""
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/checkout-sessions",
            status_code=201,
            request_headers={
                "Webhook-Id": "evt_definitely_not_a_webhook",
                "Webhook-Timestamp": "1767225600",
            },
            response_body={"id": "chk_xyz", "status": "ready_for_complete"},
        )
        # The C13 protection remains intact.
        assert event.webhook_id is None
        assert event.webhook_timestamp is None
        # Classification stays on the REST taxonomy.
        assert event.event_type == "checkout_session_created"

    async def test_new_shape_order_webhook_classifies_via_fulfillment_events(
        self, tracker, mock_writer
    ):
        """B8 end-to-end: a c5c6139-shaped order webhook with no top-
        level `status` but with a `delivered` fulfillment event must
        classify as ORDER_DELIVERED through record_http (not
        ORDER_WEBHOOK_RECEIVED, which would be the B5b regression
        without B8). Also pins that the JSON + latest_* columns
        populate."""
        event = await tracker.record_http(
            method="POST",
            url="https://platform.example.com/webhooks/orders",
            status_code=200,
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
            request_body={
                "id": "order_xyz",
                "checkout_id": "chk_a",
                "fulfillment": {
                    "events": [
                        {
                            "id": "fe_1",
                            "occurred_at": "2026-05-08T08:00:00Z",
                            "type": "shipped",
                            "line_items": [{"id": "li_1", "quantity": 1}],
                        },
                        {
                            "id": "fe_2",
                            "occurred_at": "2026-05-09T17:00:00Z",
                            "type": "delivered",
                            "line_items": [{"id": "li_1", "quantity": 1}],
                        },
                    ],
                },
            },
        )
        # Classifier picked the latest event's lifecycle.
        assert event.event_type == "order_delivered"
        # Latest-event scalars populated.
        assert event.latest_fulfillment_event_type == "delivered"
        assert event.latest_fulfillment_event_at == "2026-05-09T17:00:00Z"
        # Full array preserved for downstream queries.
        events = json.loads(event.fulfillment_events_json)
        assert len(events) == 2
        assert events[0]["id"] == "fe_1"
        assert events[1]["id"] == "fe_2"

    async def test_adjustment_only_webhook_classifies_as_returned(
        self, tracker, mock_writer
    ):
        """Refund-only webhook (no fulfillment events). Adjustment
        path drives lifecycle when fulfillment is empty."""
        event = await tracker.record_http(
            method="POST",
            url="https://platform.example.com/webhooks/orders",
            status_code=200,
            request_headers={
                "Webhook-Id": "evt_99",
                "Webhook-Timestamp": "1767225600",
            },
            request_body={
                "id": "order_xyz",
                "checkout_id": "chk_a",
                "adjustments": [
                    {
                        "id": "adj_1",
                        "type": "refund",
                        "occurred_at": "2026-05-09T20:00:00Z",
                        "status": "completed",
                        "description": "Defective item",
                    },
                ],
            },
        )
        assert event.event_type == "order_returned"
        assert event.latest_adjustment_type == "refund"
        assert event.latest_adjustment_status == "completed"
        assert event.latest_adjustment_at == "2026-05-09T20:00:00Z"

    async def test_webhook_received_when_no_lifecycle_status(
        self, tracker, mock_writer
    ):
        """Body-driven taxonomy: when a webhook is detected (by header
        or path) but the body has no recognizable lifecycle status,
        we emit ORDER_WEBHOOK_RECEIVED -- distinct from ORDER_UPDATED
        (REST-driven). This keeps webhook traffic and REST traffic
        separable in dashboards."""
        event = await tracker.record_http(
            method="POST",
            url="https://platform.example.com/hooks/abc",
            status_code=200,
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
            request_body={"id": "order_xyz"},  # no status
        )
        assert event.event_type == "order_webhook_received"

    async def test_response_body_overlays_request_body_on_conflict(
        self, tracker, mock_writer
    ):
        """When request and response both carry the same field, response
        wins — it's the merchant-confirmed state. Pinned so the merge
        order doesn't drift."""
        # Request says draft USD; response says authoritative EUR.
        request_body = {"currency": "USD", "context": {"intent": "browsing"}}
        response_body = {
            "id": "chk_456",
            "status": "incomplete",
            "currency": "EUR",
        }
        event = await tracker.record_http(
            method="POST",
            url="https://merchant.example.com/checkout-sessions",
            status_code=201,
            request_body=request_body,
            response_body=response_body,
        )
        # Response wins on the conflicting field.
        assert event.currency == "EUR"
        # Request-only field is preserved.
        assert event.context_intent == "browsing"


class TestAp2MandateRawCapture:
    """A4 — opt-in `include_ap2_raw=True` surfaces the raw `body.ap2`
    object into `ap2_mandate_raw_json` AFTER passing through
    `_redact`. Default-off; credentials redacted even when the
    operator hasn't enabled `redact_pii` generally."""

    _MERCHANT_AUTH = "eyJhbGciOiJFUzI1NiIsImtpZCI6Im1lcmNoYW50LWtleS0xIn0..sig"
    _CHECKOUT_MANDATE = (
        "eyJhbGciOiJFUzI1NiIsImtpZCI6ImJ1eWVyLWtleSIsInR5cCI6InZjK3NkLWp3dCJ9"
        ".eyJjbGFpbXMiOiJzZW5zaXRpdmUifQ.sig~disclosure1~disclosure2"
    )

    async def test_raw_absent_by_default(self, tracker, mock_writer):
        """Default tracker (no `include_ap2_raw`) leaves the raw
        column None even when a full AP2 body is present. Pinned so
        operators have to opt in to credential capture."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "ap2": {
                    "merchant_authorization": self._MERCHANT_AUTH,
                    "checkout_mandate": self._CHECKOUT_MANDATE,
                },
            },
        )
        # Safe defaults still populate (these are non-PII).
        assert event.ap2_mandate_present is True
        # But the raw column is NULL.
        assert event.ap2_mandate_raw_json is None

    async def test_raw_opt_in_redacts_credential_strings(self, mock_writer):
        """With `include_ap2_raw=True`, the raw column populates BUT
        the credential strings are scrubbed via `_redact`. The
        credential field names appear (so dashboards can confirm
        which mandates were present) but the credential values are
        `[REDACTED]`. Acceptance for the issue #8 requirement:
        'redaction must redact them before BQ insert'."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            include_ap2_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "ap2": {
                    "merchant_authorization": self._MERCHANT_AUTH,
                    "checkout_mandate": self._CHECKOUT_MANDATE,
                },
            },
        )
        assert event.ap2_mandate_raw_json is not None
        raw = json.loads(event.ap2_mandate_raw_json)
        # Credential field names preserved.
        assert "merchant_authorization" in raw
        assert "checkout_mandate" in raw
        # But values are REDACTED — the raw credential strings must
        # never appear in the column.
        assert raw["merchant_authorization"] == "[REDACTED]"
        assert raw["checkout_mandate"] == "[REDACTED]"
        # And the credential strings absolutely don't appear anywhere
        # in the serialized JSON.
        assert self._MERCHANT_AUTH not in event.ap2_mandate_raw_json
        assert self._CHECKOUT_MANDATE not in event.ap2_mandate_raw_json

    async def test_raw_opt_in_preserves_non_credential_ap2_keys(self, mock_writer):
        """A future AP2 namespace might grow non-credential fields
        (metadata about the mandate, expiry hints, etc.). With raw
        opt-in those land verbatim — only the credential field names
        are forced into the redaction set."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            include_ap2_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "ap2": {
                    "merchant_authorization": self._MERCHANT_AUTH,
                    "future_metadata": {"version": 2},
                },
            },
        )
        raw = json.loads(event.ap2_mandate_raw_json)
        assert raw["merchant_authorization"] == "[REDACTED]"
        # Non-credential AP2 field passes through unredacted.
        assert raw["future_metadata"] == {"version": 2}

    async def test_credentials_redacted_even_with_custom_pii_fields(self, mock_writer):
        """Operators who pass their own `pii_fields` list (typically
        to add custom PII keys) must NOT accidentally lose the
        AP2 credential redaction. The credential field names are
        force-included into pii_fields regardless of what the
        operator provided. Acceptance for the safety guarantee."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            # Operator provides their own list (no AP2 fields).
            pii_fields=["custom_pii_key"],
            include_ap2_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "ap2": {"merchant_authorization": self._MERCHANT_AUTH},
            },
        )
        raw = json.loads(event.ap2_mandate_raw_json)
        # Still redacted — pii_fields force-include is load-bearing.
        assert raw["merchant_authorization"] == "[REDACTED]"

    async def test_safe_defaults_populate_without_opt_in(self, tracker, mock_writer):
        """End-to-end: the safe-default columns populate from the
        un-redacted body even with the default tracker. Pins the
        un-redacted-extraction path inside record_http so SHA-256 /
        JOSE header values are real, not hashes of `[REDACTED]`."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "ap2": {"merchant_authorization": self._MERCHANT_AUTH},
            },
        )
        assert event.ap2_mandate_present is True
        keys = json.loads(event.ap2_mandate_keys_json)
        assert keys == ["merchant_authorization"]
        metadata = json.loads(event.ap2_mandate_metadata_json)
        # JOSE header decoded → kid + alg.
        assert metadata["merchant_authorization"]["kid"] == "merchant-key-1"
        assert metadata["merchant_authorization"]["alg"] == "ES256"
        # SHA-256 of the ORIGINAL credential string (not the redacted
        # `[REDACTED]` sentinel).
        import hashlib

        expected = hashlib.sha256(self._MERCHANT_AUTH.encode("utf-8")).hexdigest()
        assert metadata["merchant_authorization"]["sha256"] == expected

    async def test_safe_defaults_unaffected_by_general_pii_redact(self, mock_writer):
        """The safe-default extraction must remain correct even when
        the operator enables general PII redaction. The order in
        record_http is: AP2 safe extraction (un-redacted body) →
        raw capture (always _redact) → general extract (optionally
        redacted). Without that order, sha256 would hash `[REDACTED]`."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            redact_pii=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "ap2": {"merchant_authorization": self._MERCHANT_AUTH},
            },
        )
        # Safe-default metadata still has the JOSE-decoded kid /
        # alg, and the SHA-256 is of the real credential.
        metadata = json.loads(event.ap2_mandate_metadata_json)
        assert metadata["merchant_authorization"]["kid"] == "merchant-key-1"
        import hashlib

        expected = hashlib.sha256(self._MERCHANT_AUTH.encode("utf-8")).hexdigest()
        assert metadata["merchant_authorization"]["sha256"] == expected

    async def test_buyer_consent_extracted_without_pii_leak(self, tracker, mock_writer):
        """End-to-end buyer-consent extraction with full PII on the
        parent buyer object. The column captures only the consent
        subobject; buyer PII never appears in any extracted column."""
        event = await tracker.record_http(
            method="PUT",
            path="/checkout-sessions/chk_123",
            status_code=200,
            response_body={
                "id": "chk_123",
                "buyer": {
                    "first_name": "Jane",
                    "last_name": "Doe",
                    "email": "jane@example.com",
                    "phone_number": "+15551234567",
                    "consent": {
                        "analytics": True,
                        "marketing": False,
                    },
                },
            },
        )
        consent = json.loads(event.buyer_consent_json)
        assert consent == {"analytics": True, "marketing": False}
        # Serialize the whole BQ row and verify no buyer PII anywhere.
        row_json = json.dumps(event.to_bq_row())
        assert "Jane" not in row_json
        assert "Doe" not in row_json
        assert "jane@example.com" not in row_json
        assert "+15551234567" not in row_json


class TestSignalsRawCapture:
    """A6 — opt-in `include_signals_raw=True` surfaces the raw
    `body.signals` dict into `signals_json` AFTER `_redact`. The
    known PII signal keys (`dev.ucp.buyer_ip`, `dev.ucp.user_agent`)
    are force-included in pii_fields so values are scrubbed even
    when `redact_pii` is off or operator-provided pii_fields lists
    don't mention them."""

    async def test_raw_absent_by_default(self, tracker, mock_writer):
        """Default tracker leaves `signals_json` None even when
        signals are present. Safe-default columns still populate."""
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "signals": {
                    "dev.ucp.buyer_ip": "192.0.2.1",
                    "dev.ucp.user_agent": "Mozilla/5.0 secret-fingerprint",
                },
            },
        )
        # Safe defaults populate.
        assert event.signals_present is True
        keys = json.loads(event.signals_keys_json)
        assert keys == ["dev.ucp.buyer_ip", "dev.ucp.user_agent"]
        # Raw column is NULL.
        assert event.signals_json is None
        # And no PII values appear anywhere in the BQ row.
        row_json = json.dumps(event.to_bq_row())
        assert "192.0.2.1" not in row_json
        assert "Mozilla/5.0" not in row_json
        assert "secret-fingerprint" not in row_json

    async def test_raw_opt_in_redacts_known_pii_signals(self, mock_writer):
        """With `include_signals_raw=True`, raw column populates BUT
        the documented PII signal values are scrubbed. The signal
        key names survive (dashboards can confirm which signals were
        present) but `dev.ucp.buyer_ip` / `dev.ucp.user_agent` values
        are `[REDACTED]`."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            include_signals_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "signals": {
                    "dev.ucp.buyer_ip": "192.0.2.1",
                    "dev.ucp.user_agent": "Mozilla/5.0 (privacy)",
                },
            },
        )
        assert event.signals_json is not None
        raw = json.loads(event.signals_json)
        # Both keys preserved.
        assert "dev.ucp.buyer_ip" in raw
        assert "dev.ucp.user_agent" in raw
        # But values are redacted.
        assert raw["dev.ucp.buyer_ip"] == "[REDACTED]"
        assert raw["dev.ucp.user_agent"] == "[REDACTED]"
        # And the original values never appear in the serialized
        # raw column (or anywhere in the BQ row).
        row_json = json.dumps(event.to_bq_row())
        assert "192.0.2.1" not in row_json
        assert "Mozilla/5.0" not in row_json
        assert "privacy" not in row_json

    async def test_raw_opt_in_preserves_non_pii_signals(self, mock_writer):
        """Operators may ship merchant-specific signals (e.g.
        `dev.merchant.session_count`) that are NOT PII. Those should
        land verbatim in the raw column; only the documented PII
        signal keys are force-redacted by default."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            include_signals_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "signals": {
                    "dev.ucp.buyer_ip": "192.0.2.1",
                    "dev.merchant.session_count": 5,
                    "dev.merchant.risk_score": 0.87,
                },
            },
        )
        raw = json.loads(event.signals_json)
        # PII signal redacted.
        assert raw["dev.ucp.buyer_ip"] == "[REDACTED]"
        # Non-PII signals pass through unredacted.
        assert raw["dev.merchant.session_count"] == 5
        assert raw["dev.merchant.risk_score"] == 0.87

    async def test_pii_signals_redacted_even_with_custom_pii_fields(self, mock_writer):
        """Operators who pass their own `pii_fields` list (often to
        add merchant-specific PII keys) must NOT accidentally lose
        the documented signal redaction. The known PII signal keys
        are force-OR-ed into pii_fields regardless. Same safety
        guarantee as A4 with AP2 credentials."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            # Operator provides a custom list (no UCP signals).
            pii_fields=["dev.merchant.custom_pii"],
            include_signals_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "signals": {
                    "dev.ucp.buyer_ip": "192.0.2.1",
                    "dev.ucp.user_agent": "Mozilla/5.0",
                    "dev.merchant.custom_pii": "operator-specific-secret",
                },
            },
        )
        raw = json.loads(event.signals_json)
        # Documented PII signals still redacted.
        assert raw["dev.ucp.buyer_ip"] == "[REDACTED]"
        assert raw["dev.ucp.user_agent"] == "[REDACTED]"
        # Operator-added field also redacted.
        assert raw["dev.merchant.custom_pii"] == "[REDACTED]"

    async def test_operator_extension_for_new_pii_signal(self, mock_writer):
        """Operators on platforms shipping additional reverse-domain
        PII signals (per the spec's `additionalProperties: true`)
        can extend pii_fields to redact those. Acceptance for the
        issue #8 requirement that operator-configured reverse-domain
        keys are redactable."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            # Note: extending pii_fields here also extends the
            # default set (the docs default applies when no list is
            # given). When the operator provides their own list, the
            # documented signals are force-ORed in anyway.
            pii_fields=[
                "email",
                "phone",
                "first_name",
                "dev.partner.fingerprint",
            ],
            include_signals_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "signals": {
                    "dev.ucp.buyer_ip": "192.0.2.1",
                    "dev.partner.fingerprint": "fp-secret-data",
                },
            },
        )
        raw = json.loads(event.signals_json)
        assert raw["dev.ucp.buyer_ip"] == "[REDACTED]"
        # Operator-extended signal also redacted.
        assert raw["dev.partner.fingerprint"] == "[REDACTED]"

    async def test_safe_defaults_unaffected_by_redact_pii(self, mock_writer):
        """The safe-default columns (signals_present,
        signals_keys_json) carry only key names, so they're the same
        regardless of `redact_pii` — the key set in the dict doesn't
        change when values are redacted. Pin that enabling general
        redaction doesn't accidentally drop these columns."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            redact_pii=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "signals": {
                    "dev.ucp.buyer_ip": "10.0.0.1",
                    "dev.ucp.user_agent": "UA-secret",
                },
            },
        )
        assert event.signals_present is True
        keys = json.loads(event.signals_keys_json)
        assert keys == ["dev.ucp.buyer_ip", "dev.ucp.user_agent"]
        # No PII anywhere.
        row_json = json.dumps(event.to_bq_row())
        assert "10.0.0.1" not in row_json
        assert "UA-secret" not in row_json

    async def test_no_signals_object_no_raw_column(self, tracker, mock_writer):
        """No signals dict on the body → no raw column either, even
        with include_signals_raw on. We don't fabricate an empty
        signals dict — three-state NULL preserved."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            include_signals_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={"id": "chk_123"},
        )
        assert event.signals_json is None
        assert event.signals_present is None

    async def test_non_string_keys_do_not_crash_raw_serialization(self, mock_writer):
        """Reviewer's PR-24 repro: a malformed sender ships a dict
        with non-string keys (int, None, tuple). Without the
        isinstance guard in _redact, `.lower()` would crash with
        AttributeError. And tuple keys would crash json.dumps
        regardless. The row must still record without crashing —
        non-string keys are dropped from the raw column, well-formed
        string keys survive and get redacted normally."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            include_signals_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "signals": {
                    42: "smuggled-via-int-key",
                    None: "smuggled-via-none-key",
                    ("tuple",): "smuggled-via-tuple-key",
                    "dev.ucp.buyer_ip": "192.0.2.1",
                },
            },
        )
        # The row was recorded (no crash).
        mock_writer.enqueue.assert_awaited_once()
        # Raw column populated with the string-keyed entries only.
        raw = json.loads(event.signals_json)
        assert "dev.ucp.buyer_ip" in raw
        # Documented PII signal value redacted.
        assert raw["dev.ucp.buyer_ip"] == "[REDACTED]"
        # Non-string-keyed entries dropped — their smuggled values
        # never reach the column.
        row_json = json.dumps(event.to_bq_row())
        assert "smuggled-via-int-key" not in row_json
        assert "smuggled-via-none-key" not in row_json
        assert "smuggled-via-tuple-key" not in row_json

    async def test_only_non_string_keys_omits_raw_column(self, mock_writer):
        """A signals dict with ONLY non-string keys produces no raw
        column (nothing serializable survives the filter). Safe-
        default columns reflect the original dict's emptiness —
        signals_present True (we observed the delivery), but
        signals_keys_json absent (no string keys to list) and
        signals_json absent."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            include_signals_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "signals": {42: "x", None: "y"},
            },
        )
        # Delivery observed but no serializable keys.
        assert event.signals_present is True
        assert event.signals_keys_json is None
        assert event.signals_json is None

    async def test_non_string_keys_in_ap2_raw_capture(self, mock_writer):
        """Parallel reviewer-repro fix on the A4 side: a malformed
        sender shipping a non-string key inside body.ap2 must not
        crash the raw AP2 capture path. Same isinstance guard +
        string-key filter as signals."""
        tracker = UCPAnalyticsTracker(
            project_id="test",
            include_ap2_raw=True,
        )
        event = await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            response_body={
                "id": "chk_123",
                "ap2": {
                    42: "smuggled-int",
                    ("tuple",): "smuggled-tuple",
                    "merchant_authorization": "eyJhbGciOiJFUzI1NiJ9..sig",
                },
            },
        )
        mock_writer.enqueue.assert_awaited_once()
        raw = json.loads(event.ap2_mandate_raw_json)
        # String-keyed credential still redacted.
        assert raw["merchant_authorization"] == "[REDACTED]"
        # Non-string-keyed smuggled entries dropped.
        row_json = json.dumps(event.to_bq_row())
        assert "smuggled-int" not in row_json
        assert "smuggled-tuple" not in row_json


class TestPIIRedaction:
    async def test_redacts_configured_fields(self, mock_writer):
        tracker = UCPAnalyticsTracker(
            project_id="test",
            redact_pii=True,
        )
        await tracker.record_http(
            method="PUT",
            path="/checkout-sessions/chk_123",
            status_code=200,
            response_body={
                "id": "chk_123",
                "status": "ready_for_complete",
                "buyer": {
                    "email": "jane@example.com",
                    "phone": "555-1234",
                    "first_name": "Jane",
                    "full_name": "Jane Doe",
                },
            },
        )

        # The event should be recorded (no crash)
        mock_writer.enqueue.assert_awaited_once()

    def test_redact_handles_non_string_dict_keys(self):
        """`_redact` is dict-recursive and reads `k.lower()` on every
        key. Non-string keys (int / None / tuple) raised
        AttributeError before this fix. After: non-string keys are
        passed through unchanged (they can't match pii_fields, which
        holds strings) — the recursion only redacts string-key
        values that match. Verified at the unit level so any future
        caller that hands `_redact` a non-string-key dict (Pydantic
        models, framework-serialized structures, etc.) doesn't
        crash."""
        tracker = UCPAnalyticsTracker(project_id="test", redact_pii=True)
        # All three flavors of non-string keys plus a real string
        # key that should still get redacted.
        result = tracker._redact(
            {
                42: "smuggled-int",
                None: "smuggled-none",
                ("a", "b"): "smuggled-tuple",
                "email": "leaked@example.com",
            }
        )
        # No crash. The string-keyed PII field is redacted; non-
        # string keys keep their values.
        assert result == {
            42: "smuggled-int",
            None: "smuggled-none",
            ("a", "b"): "smuggled-tuple",
            "email": "[REDACTED]",
        }

    def test_redact_recurses_into_non_string_keyed_dict_values(self):
        """The value side of a non-string-keyed entry should still
        be walked recursively so nested string-keyed PII is caught."""
        tracker = UCPAnalyticsTracker(project_id="test", redact_pii=True)
        result = tracker._redact(
            {
                42: {"email": "nested@example.com", "ok": "fine"},
            }
        )
        # Nested PII still redacted under the non-string key.
        assert result[42] == {"email": "[REDACTED]", "ok": "fine"}

    async def test_redact_nested(self, mock_writer):
        tracker = UCPAnalyticsTracker(
            project_id="test",
            redact_pii=True,
        )
        data = {
            "buyer": {"email": "secret@test.com"},
            "items": [{"email": "also@secret.com"}],
        }
        redacted = tracker._redact(data)

        assert redacted["buyer"]["email"] == "[REDACTED]"
        assert redacted["items"][0]["email"] == "[REDACTED]"

    async def test_redact_preserves_non_pii(self, mock_writer):
        tracker = UCPAnalyticsTracker(
            project_id="test",
            redact_pii=True,
        )
        data = {"id": "chk_123", "status": "incomplete", "email": "secret"}
        redacted = tracker._redact(data)

        assert redacted["id"] == "chk_123"
        assert redacted["status"] == "incomplete"
        assert redacted["email"] == "[REDACTED]"

    async def test_no_redaction_when_disabled(self, mock_writer):
        tracker = UCPAnalyticsTracker(project_id="test", redact_pii=False)
        await tracker.record_http(
            method="PUT",
            path="/checkout-sessions/chk_123",
            status_code=200,
            response_body={
                "id": "chk_123",
                "buyer": {"email": "jane@example.com"},
            },
        )

        mock_writer.enqueue.assert_awaited_once()


class TestFlushAndClose:
    async def test_flush_delegates(self, tracker, mock_writer):
        await tracker.flush()
        mock_writer.flush.assert_awaited_once()

    async def test_close_delegates(self, tracker, mock_writer):
        await tracker.close()
        mock_writer.close.assert_awaited_once()

    async def test_close_drains_pending_tasks(self, tracker, mock_writer):
        """close() should await in-flight tasks before flushing."""
        completed = []

        async def slow_work():
            await asyncio.sleep(0.01)
            completed.append(True)

        task = asyncio.create_task(slow_work())
        tracker.register_pending_task(task)

        await tracker.close()
        assert completed == [True]
        assert len(tracker._pending_tasks) == 0
