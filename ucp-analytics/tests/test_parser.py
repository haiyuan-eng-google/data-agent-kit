"""Tests for UCPResponseParser."""

import json

from ucp_analytics.events import UCPEventType
from ucp_analytics.parser import UCPResponseParser


class TestClassify:
    def test_discovery(self):
        assert (
            UCPResponseParser.classify("GET", "/.well-known/ucp", 200, None)
            == UCPEventType.PROFILE_DISCOVERED
        )

    def test_create_checkout(self):
        assert (
            UCPResponseParser.classify("POST", "/checkout-sessions", 201, {})
            == UCPEventType.CHECKOUT_SESSION_CREATED
        )

    def test_update_checkout(self):
        assert (
            UCPResponseParser.classify(
                "PUT",
                "/checkout-sessions/chk_123",
                200,
                {"status": "ready_for_complete"},
            )
            == UCPEventType.CHECKOUT_SESSION_UPDATED
        )

    def test_update_escalation(self):
        assert (
            UCPResponseParser.classify(
                "PUT",
                "/checkout-sessions/chk_123",
                200,
                {"status": "requires_escalation"},
            )
            == UCPEventType.CHECKOUT_ESCALATION
        )

    def test_complete_checkout(self):
        assert (
            UCPResponseParser.classify(
                "POST", "/checkout-sessions/chk_123/complete", 200, {}
            )
            == UCPEventType.CHECKOUT_SESSION_COMPLETED
        )

    def test_cancel_checkout(self):
        assert (
            UCPResponseParser.classify(
                "POST", "/checkout-sessions/chk_123/cancel", 200, {}
            )
            == UCPEventType.CHECKOUT_SESSION_CANCELED
        )

    def test_get_checkout(self):
        assert (
            UCPResponseParser.classify("GET", "/checkout-sessions/chk_123", 200, {})
            == UCPEventType.CHECKOUT_SESSION_GET
        )

    # --- Cart endpoints ---

    def test_create_cart(self):
        assert (
            UCPResponseParser.classify("POST", "/carts", 201, {})
            == UCPEventType.CART_CREATED
        )

    def test_get_cart(self):
        assert (
            UCPResponseParser.classify("GET", "/carts/cart_123", 200, {})
            == UCPEventType.CART_GET
        )

    def test_update_cart(self):
        assert (
            UCPResponseParser.classify("PUT", "/carts/cart_123", 200, {})
            == UCPEventType.CART_UPDATED
        )

    def test_cancel_cart(self):
        assert (
            UCPResponseParser.classify("POST", "/carts/cart_123/cancel", 200, {})
            == UCPEventType.CART_CANCELED
        )

    def test_catalog_search(self):
        assert (
            UCPResponseParser.classify("POST", "/catalog/search", 200, {})
            == UCPEventType.CATALOG_SEARCH
        )

    def test_catalog_lookup(self):
        assert (
            UCPResponseParser.classify("POST", "/catalog/lookup", 200, {})
            == UCPEventType.CATALOG_LOOKUP
        )

    def test_catalog_product_get(self):
        assert (
            UCPResponseParser.classify("POST", "/catalog/product", 200, {})
            == UCPEventType.CATALOG_PRODUCT_GET
        )

    def test_catalog_search_under_base_path(self):
        # OpenAPI paths are relative to the discovered REST endpoint;
        # real merchants commonly mount UCP under /ucp/v1, /api/v2, …
        assert (
            UCPResponseParser.classify("POST", "/ucp/v1/catalog/search", 200, {})
            == UCPEventType.CATALOG_SEARCH
        )

    # --- Other ---

    def test_error(self):
        # Error fallback applies to paths that don't match a specific UCP pattern
        assert (
            UCPResponseParser.classify("POST", "/some/unknown/path", 500, {})
            == UCPEventType.ERROR
        )

    def test_order(self):
        assert (
            UCPResponseParser.classify("POST", "/orders", 201, {})
            == UCPEventType.ORDER_CREATED
        )

    def test_simulate_shipping(self):
        assert (
            UCPResponseParser.classify(
                "POST", "/testing/simulate-shipping/order_123", 200, {}
            )
            == UCPEventType.ORDER_SHIPPED
        )

    # --- Order lifecycle (status-based) ---

    def test_order_delivered(self):
        assert (
            UCPResponseParser.classify(
                "GET", "/orders/order_123", 200, {"status": "delivered"}
            )
            == UCPEventType.ORDER_DELIVERED
        )

    def test_order_returned(self):
        assert (
            UCPResponseParser.classify(
                "GET", "/orders/order_123", 200, {"status": "returned"}
            )
            == UCPEventType.ORDER_RETURNED
        )

    def test_order_canceled(self):
        assert (
            UCPResponseParser.classify(
                "GET", "/orders/order_123", 200, {"status": "canceled"}
            )
            == UCPEventType.ORDER_CANCELED
        )

    def test_order_canceled_british_spelling(self):
        assert (
            UCPResponseParser.classify(
                "GET", "/orders/order_123", 200, {"status": "cancelled"}
            )
            == UCPEventType.ORDER_CANCELED
        )

    # --- Webhook paths ---

    def test_webhook_order_delivered(self):
        assert (
            UCPResponseParser.classify("POST", "/webhooks/order-delivered", 200, {})
            == UCPEventType.ORDER_DELIVERED
        )

    def test_webhook_order_returned(self):
        assert (
            UCPResponseParser.classify("POST", "/webhook/order-returned", 200, {})
            == UCPEventType.ORDER_RETURNED
        )

    def test_webhook_order_canceled(self):
        assert (
            UCPResponseParser.classify("POST", "/webhooks/order_canceled", 200, {})
            == UCPEventType.ORDER_CANCELED
        )

    # --- Identity sub-paths ---

    def test_identity_initiated(self):
        assert (
            UCPResponseParser.classify("POST", "/identity", 200, {})
            == UCPEventType.IDENTITY_LINK_INITIATED
        )

    def test_identity_callback(self):
        assert (
            UCPResponseParser.classify("GET", "/identity/callback", 200, {})
            == UCPEventType.IDENTITY_LINK_COMPLETED
        )

    def test_oauth_callback(self):
        assert (
            UCPResponseParser.classify("GET", "/oauth/callback", 200, {})
            == UCPEventType.IDENTITY_LINK_COMPLETED
        )

    def test_identity_revoke(self):
        assert (
            UCPResponseParser.classify("POST", "/identity/revoke", 200, {})
            == UCPEventType.IDENTITY_LINK_REVOKED
        )

    def test_identity_delete(self):
        assert (
            UCPResponseParser.classify("DELETE", "/identity/link_123", 200, {})
            == UCPEventType.IDENTITY_LINK_REVOKED
        )

    # --- OAuth 2.0 + OpenID identity-linking flow ---

    def test_oauth_authorization_metadata_discovery(self):
        # RFC 8414 — auth-server metadata discovery is the start of an
        # identity-linking flow.
        assert (
            UCPResponseParser.classify(
                "GET", "/.well-known/oauth-authorization-server", 200, {}
            )
            == UCPEventType.IDENTITY_LINK_INITIATED
        )

    def test_openid_configuration_discovery(self):
        assert (
            UCPResponseParser.classify(
                "GET", "/.well-known/openid-configuration", 200, {}
            )
            == UCPEventType.IDENTITY_LINK_INITIATED
        )

    def test_oauth_protected_resource_discovery(self):
        # RFC 9728 — protected-resource metadata.
        assert (
            UCPResponseParser.classify(
                "GET", "/.well-known/oauth-protected-resource", 200, {}
            )
            == UCPEventType.IDENTITY_LINK_INITIATED
        )

    def test_oauth_discovery_under_mounted_base(self):
        # OAuth discovery paths can be mounted under a prefix the same
        # way other UCP REST paths can.
        assert (
            UCPResponseParser.classify(
                "GET",
                "/api/v1/.well-known/oauth-authorization-server",
                200,
                {},
            )
            == UCPEventType.IDENTITY_LINK_INITIATED
        )

    def test_oauth2_authorize_initiated(self):
        assert (
            UCPResponseParser.classify("GET", "/oauth2/authorize", 200, {})
            == UCPEventType.IDENTITY_LINK_INITIATED
        )

    def test_oauth2_token_completed(self):
        # Token issuance is the moment the identity link becomes usable.
        assert (
            UCPResponseParser.classify("POST", "/oauth2/token", 200, {})
            == UCPEventType.IDENTITY_LINK_COMPLETED
        )

    def test_oauth2_revoke_revoked(self):
        assert (
            UCPResponseParser.classify("POST", "/oauth2/revoke", 200, {})
            == UCPEventType.IDENTITY_LINK_REVOKED
        )

    def test_oauth2_jwks_initiated(self):
        # JWKS endpoint is part of the OAuth metadata surface — fetch
        # before any token validation. Keep it under INITIATED rather
        # than minting a separate event type for v0.
        assert (
            UCPResponseParser.classify("GET", "/oauth2/jwks", 200, {})
            == UCPEventType.IDENTITY_LINK_INITIATED
        )

    def test_oauth2_token_under_mounted_base(self):
        assert (
            UCPResponseParser.classify("POST", "/api/v1/oauth2/token", 200, {})
            == UCPEventType.IDENTITY_LINK_COMPLETED
        )


class TestExtract:
    # Sample checkout response using SDK/samples-aligned format
    SAMPLE_CHECKOUT_RESPONSE = {
        "ucp": {
            "version": "2026-01-11",
            "capabilities": [
                {"name": "dev.ucp.shopping.checkout", "version": "2026-01-11"},
                {
                    "name": "dev.ucp.shopping.fulfillment",
                    "version": "2026-01-11",
                    "extends": "dev.ucp.shopping.checkout",
                },
            ],
        },
        "id": "chk_abc123",
        "status": "ready_for_complete",
        "currency": "USD",
        "line_items": [
            {
                "id": "li_1",
                "item": {
                    "id": "item_1",
                    "title": "Rose Bouquet",
                    "price": 2500,
                },
                "quantity": 2,
            },
        ],
        "totals": [
            {"type": "subtotal", "amount": 5000},
            {"type": "tax", "amount": 400},
            {"type": "fulfillment", "amount": 599},
            {"type": "total", "amount": 5999},
        ],
        "payment": {
            "handlers": [
                {
                    "id": "google_pay",
                    "name": "com.google.pay",
                    "version": "2026-01-11",
                    "spec": "https://example.com/spec",
                    "config_schema": "https://example.com/schema",
                    "instrument_schemas": [],
                    "config": {},
                },
            ],
            "instruments": [
                {
                    "id": "instr_1",
                    "handler_id": "google_pay",
                    "type": "wallet",
                    "brand": "google_pay",
                },
            ],
        },
        "fulfillment": {
            "methods": [
                {
                    "id": "method_1",
                    "type": "shipping",
                    "line_item_ids": ["li_1"],
                    "destinations": [
                        {
                            "id": "dest_1",
                            "address_country": "US",
                            "postal_code": "94043",
                        },
                    ],
                }
            ]
        },
        "discounts": {
            "codes": ["SUMMER20"],
            "applied": [
                {"code": "SUMMER20", "title": "Summer Sale", "amount": 500},
            ],
        },
        "expires_at": "2026-01-12T00:00:00Z",
        "messages": [
            {
                "type": "error",
                "code": "missing",
                "content": "Phone required",
                "severity": "recoverable",
            },
        ],
    }

    def test_extract_checkout_fields(self):
        fields = UCPResponseParser.extract(self.SAMPLE_CHECKOUT_RESPONSE)

        assert fields["checkout_session_id"] == "chk_abc123"
        assert fields["checkout_status"] == "ready_for_complete"
        assert fields["currency"] == "USD"
        assert fields["subtotal_amount"] == 5000
        assert fields["tax_amount"] == 400
        assert fields["fulfillment_amount"] == 599
        assert fields["total_amount"] == 5999
        assert fields["line_item_count"] == 1
        assert fields["ucp_version"] == "2026-01-11"
        assert fields["payment_handler_id"] == "google_pay"
        assert fields["payment_instrument_type"] == "wallet"
        assert fields["fulfillment_type"] == "shipping"
        assert fields["fulfillment_destination_country"] == "US"
        assert fields["error_code"] == "missing"
        assert fields["error_severity"] == "recoverable"
        assert fields["expires_at"] == "2026-01-12T00:00:00Z"

    def test_extract_capabilities_array(self):
        """SDK/samples: capabilities are an array with name fields."""
        fields = UCPResponseParser.extract(self.SAMPLE_CHECKOUT_RESPONSE)
        assert "capabilities_json" in fields
        caps = json.loads(fields["capabilities_json"])
        names = [c["name"] for c in caps]
        assert "dev.ucp.shopping.checkout" in names
        assert "dev.ucp.shopping.fulfillment" in names

    def test_extract_capabilities_object_keyed_compat(self):
        """Robustness: object-keyed capabilities are normalized to array."""
        body = {
            "ucp": {
                "version": "2026-01-11",
                "capabilities": {
                    "dev.ucp.shopping.checkout": [{"version": "2026-01-11"}],
                    "dev.ucp.shopping.fulfillment": [{"version": "2026-01-11"}],
                },
            },
        }
        fields = UCPResponseParser.extract(body)
        caps = json.loads(fields["capabilities_json"])
        assert len(caps) == 2
        names = [c["name"] for c in caps]
        assert "dev.ucp.shopping.checkout" in names

    def test_extract_payment_instruments(self):
        """Spec: payment.instruments[] with handler_id."""
        body = {
            "payment": {
                "instruments": [
                    {
                        "id": "instr_1",
                        "handler_id": "com.stripe",
                        "type": "card",
                        "brand": "visa",
                    },
                ]
            }
        }
        fields = UCPResponseParser.extract(body)
        assert fields["payment_handler_id"] == "com.stripe"
        assert fields["payment_instrument_type"] == "card"
        assert fields["payment_brand"] == "visa"

    def test_extract_payment_handlers_only(self):
        """Checkout payment with only handlers (no instruments selected yet)."""
        body = {
            "payment": {
                "handlers": [
                    {
                        "id": "gpay",
                        "name": "google.pay",
                        "version": "2026-01-11",
                        "spec": "https://example.com",
                        "config_schema": "https://example.com",
                        "instrument_schemas": [],
                        "config": {},
                    },
                ]
            }
        }
        fields = UCPResponseParser.extract(body)
        assert fields["payment_handler_id"] == "gpay"

    def test_extract_discovery_payment_handlers(self):
        """SDK: discovery has payment.handlers at top level (sibling of ucp)."""
        body = {
            "ucp": {
                "version": "2026-01-11",
                "capabilities": [
                    {"name": "dev.ucp.shopping.checkout", "version": "2026-01-11"},
                ],
            },
            "payment": {
                "handlers": [
                    {
                        "id": "mock_handler",
                        "name": "com.mock.payment",
                        "version": "2026-01-11",
                    },
                ],
            },
        }
        fields = UCPResponseParser.extract(body)
        assert fields["payment_handler_id"] == "mock_handler"
        assert fields["ucp_version"] == "2026-01-11"

    def test_extract_payment_data(self):
        """payment_data from complete request/response."""
        body = {
            "payment_data": {
                "handler_id": "com.stripe",
                "type": "card",
                "brand": "visa",
            }
        }
        fields = UCPResponseParser.extract(body)
        assert fields["payment_handler_id"] == "com.stripe"

    def test_extract_discounts(self):
        """Spec: discount extension with codes and applied."""
        body = {
            "discounts": {
                "codes": ["SAVE10", "LOYALTY"],
                "applied": [
                    {"code": "SAVE10", "title": "Save 10%", "amount": 1000},
                ],
            }
        }
        fields = UCPResponseParser.extract(body)
        codes = json.loads(fields["discount_codes_json"])
        assert codes == ["SAVE10", "LOYALTY"]
        applied = json.loads(fields["discount_applied_json"])
        assert applied[0]["code"] == "SAVE10"

    # --- Context (request-body Context object) ---

    def test_extract_context_on_checkout_create(self):
        """Checkout-create requests carry a top-level `context` object
        with intent, language, currency, and eligibility per
        source/schemas/shopping/types/context.json."""
        body = {
            "context": {
                "intent": "buy a birthday gift for mom",
                "language": "en-US",
                "currency": "USD",
                "eligibility": [
                    "dev.example.loyalty_member",
                    "dev.example.first_time_buyer",
                ],
            },
            "line_items": [
                {"item": {"id": "sku_rose"}, "quantity": 1},
            ],
        }
        fields = UCPResponseParser.extract(body)
        assert fields["context_intent"] == "buy a birthday gift for mom"
        assert fields["context_language"] == "en-US"
        assert fields["context_currency"] == "USD"
        assert "dev.example.loyalty_member" in fields["context_eligibility_json"]
        assert "dev.example.first_time_buyer" in fields["context_eligibility_json"]

    def test_extract_context_on_cart_create(self):
        """Cart-create requests carry the same Context object shape."""
        body = {
            "context": {
                "intent": "stock up for the week",
                "language": "fr-CA",
                "currency": "CAD",
            },
            "line_items": [{"item": {"id": "sku_milk"}, "quantity": 2}],
        }
        fields = UCPResponseParser.extract(body)
        assert fields["context_intent"] == "stock up for the week"
        assert fields["context_language"] == "fr-CA"
        assert fields["context_currency"] == "CAD"
        # No eligibility on this body — the column should be absent.
        assert "context_eligibility_json" not in fields

    def test_extract_context_on_catalog_search(self):
        """Catalog-search requests use the same Context shape; intent
        in particular is the high-value signal for relevance analytics."""
        body = {
            "context": {
                "intent": "vegan running shoes under 100 dollars",
                "language": "en",
                "currency": "USD",
            },
            "query": "running shoes",
        }
        fields = UCPResponseParser.extract(body)
        assert fields["context_intent"] == "vegan running shoes under 100 dollars"
        assert fields["context_language"] == "en"
        assert fields["context_currency"] == "USD"

    def test_extract_context_partial(self):
        """A context object may carry only a subset of properties; we
        only populate the columns that are present in the source body."""
        body = {"context": {"language": "ja-JP"}}
        fields = UCPResponseParser.extract(body)
        assert fields["context_language"] == "ja-JP"
        assert "context_intent" not in fields
        assert "context_currency" not in fields
        assert "context_eligibility_json" not in fields

    def test_extract_context_address_fields_not_captured_yet(self):
        """Address fields on context (address_country, address_region,
        postal_code) are PII and intentionally deferred to a later slice
        with the redaction policy. This test pins that current behavior
        so no one accidentally surfaces them as scalar columns without
        also wiring redaction."""
        body = {
            "context": {
                "intent": "ship to office",
                "address_country": "US",
                "address_region": "CA",
                "postal_code": "94043",
            },
        }
        fields = UCPResponseParser.extract(body)
        assert fields["context_intent"] == "ship to office"
        assert "context_address_country" not in fields
        assert "context_address_region" not in fields
        assert "context_postal_code" not in fields

    def test_extract_no_context_object(self):
        """Bodies without a context object don't populate any of the
        new columns."""
        body = {"id": "chk_123", "status": "ready_for_complete"}
        fields = UCPResponseParser.extract(body)
        assert "context_intent" not in fields
        assert "context_language" not in fields
        assert "context_currency" not in fields
        assert "context_eligibility_json" not in fields

    # --- payment_handlers[*].available_instruments ---

    def test_extract_payment_available_instruments_array_shape(self):
        """body.ucp.payment_handlers as an array of handler objects."""
        body = {
            "ucp": {
                "version": "2026-04-08",
                "payment_handlers": [
                    {
                        "id": "gpay",
                        "name": "Google Pay",
                        "available_instruments": [
                            {"type": "card", "brand": "visa"},
                            {"type": "card", "brand": "mastercard"},
                        ],
                    },
                    {
                        "id": "stripe",
                        "name": "Stripe",
                        "available_instruments": [
                            {"type": "card", "brand": "amex"},
                        ],
                    },
                ],
            },
        }
        fields = UCPResponseParser.extract(body)
        instruments = json.loads(fields["payment_available_instruments_json"])
        assert len(instruments) == 2
        gpay = next(h for h in instruments if h.get("id") == "gpay")
        assert len(gpay["available_instruments"]) == 2
        stripe = next(h for h in instruments if h.get("id") == "stripe")
        assert stripe["available_instruments"][0]["brand"] == "amex"

    def test_extract_payment_available_instruments_dict_keyed_shape(self):
        """body.ucp.payment_handlers as a dict keyed by handler name —
        the same registry shape capabilities use."""
        body = {
            "ucp": {
                "version": "2026-04-08",
                "payment_handlers": {
                    "dev.example.gpay": [
                        {
                            "id": "gpay",
                            "available_instruments": [{"type": "wallet"}],
                        }
                    ],
                },
            },
        }
        fields = UCPResponseParser.extract(body)
        instruments = json.loads(fields["payment_available_instruments_json"])
        assert len(instruments) == 1
        # _normalize_registry stamps the dict key as `name` on the entry.
        assert instruments[0]["name"] == "dev.example.gpay"
        assert instruments[0]["available_instruments"][0]["type"] == "wallet"

    def test_extract_payment_handlers_without_instruments_dropped(self):
        """A handler that doesn't declare available_instruments isn't
        useful for the instruments-offered KPI; drop it from the
        stored payload so the column reflects only handlers that
        publish a registry."""
        body = {
            "ucp": {
                "payment_handlers": [
                    {"id": "gpay", "available_instruments": [{"type": "card"}]},
                    {"id": "applepay"},  # no available_instruments
                    {"id": "stripe", "available_instruments": []},  # empty
                ],
            },
        }
        fields = UCPResponseParser.extract(body)
        instruments = json.loads(fields["payment_available_instruments_json"])
        assert len(instruments) == 1
        assert instruments[0]["id"] == "gpay"

    def test_extract_payment_handlers_with_malformed_instruments_dropped(
        self,
    ):
        """payment_handler.json defines available_instruments as an
        array. Any other shape (string, dict, scalar) is malformed
        and would break dashboard assumptions around
        JSON_QUERY_ARRAY(...available_instruments...). Drop the
        handler so a single bad sender can't corrupt the column
        contract for everyone querying the table."""
        body = {
            "ucp": {
                "payment_handlers": [
                    {"id": "gpay", "available_instruments": [{"type": "card"}]},
                    # String instead of array — malformed.
                    {"id": "bad-string", "available_instruments": "card"},
                    # Dict instead of array — malformed.
                    {"id": "bad-dict", "available_instruments": {"type": "card"}},
                    # Scalar truthy values that pre-fix would have
                    # survived `if h.get(...)`.
                    {"id": "bad-int", "available_instruments": 1},
                    {"id": "bad-bool", "available_instruments": True},
                ],
            },
        }
        fields = UCPResponseParser.extract(body)
        instruments = json.loads(fields["payment_available_instruments_json"])
        assert len(instruments) == 1
        assert instruments[0]["id"] == "gpay"

    def test_extract_payment_available_instruments_does_not_misread_payment_object(
        self,
    ):
        """The new column sources from body.ucp.payment_handlers, not
        body.payment.handlers — the latter is selected/submitted
        instruments on a checkout response, not the handler-declaration
        registry. Pin this so the column doesn't accidentally start
        sourcing from the wrong path."""
        body = {
            "ucp": {"version": "2026-04-08"},
            # body.payment.handlers — NOT the source for this column.
            "payment": {
                "handlers": [{"id": "gpay", "type": "wallet", "brand": "google_pay"}]
            },
        }
        fields = UCPResponseParser.extract(body)
        # Existing payment_handler_id still extracted for backwards compat.
        assert fields["payment_handler_id"] == "gpay"
        # But the new available-instruments column stays absent because
        # ucp.payment_handlers is missing.
        assert "payment_available_instruments_json" not in fields

    def test_extract_no_payment_handlers_in_ucp(self):
        """No ucp.payment_handlers → column absent."""
        body = {"ucp": {"version": "2026-04-08"}}
        fields = UCPResponseParser.extract(body)
        assert "payment_available_instruments_json" not in fields

    # --- messages[]: per-severity code lists + identity_optional ---

    def test_extract_message_info_codes(self):
        """Info-severity codes collect into message_info_codes_json,
        order-preserved and deduped."""
        body = {
            "messages": [
                {"type": "info", "code": "tax_rounded_up"},
                {"type": "info", "code": "identity_optional"},
                {"type": "info", "code": "tax_rounded_up"},  # duplicate
            ]
        }
        fields = UCPResponseParser.extract(body)
        codes = json.loads(fields["message_info_codes_json"])
        assert codes == ["tax_rounded_up", "identity_optional"]
        assert fields["identity_optional_present"] is True

    def test_extract_message_warning_codes(self):
        body = {
            "messages": [
                {"type": "warning", "code": "shipping_delayed"},
                {"type": "warning", "code": "stock_low"},
                {"type": "warning", "code": "shipping_delayed"},  # duplicate
            ]
        }
        fields = UCPResponseParser.extract(body)
        codes = json.loads(fields["message_warning_codes_json"])
        assert codes == ["shipping_delayed", "stock_low"]
        # No info codes → no flag.
        assert "identity_optional_present" not in fields

    def test_extract_mixed_severities_in_one_pass(self):
        """A real checkout response carries multiple severities at once.
        Single-pass walk must populate all three columns plus the
        legacy error_code from the first error."""
        body = {
            "messages": [
                {
                    "type": "info",
                    "code": "tax_rounded_up",
                    "content": "Tax rounded up by $0.01",
                },
                {
                    "type": "warning",
                    "code": "shipping_delayed",
                    "content": "Shipping may be delayed",
                },
                {
                    "type": "error",
                    "code": "missing_email",
                    "content": "Email is required",
                    "severity": "recoverable",
                },
                {"type": "info", "code": "identity_optional"},
                {
                    "type": "error",
                    "code": "should_not_overwrite_first_error",
                    "content": "...",
                },
            ]
        }
        fields = UCPResponseParser.extract(body)
        # Legacy error_* columns: first error only.
        assert fields["error_code"] == "missing_email"
        assert fields["error_severity"] == "recoverable"
        # Per-severity lists.
        info = json.loads(fields["message_info_codes_json"])
        assert info == ["tax_rounded_up", "identity_optional"]
        warnings = json.loads(fields["message_warning_codes_json"])
        assert warnings == ["shipping_delayed"]
        # Identity-optional flag picked up from the info pass.
        assert fields["identity_optional_present"] is True

    def test_identity_optional_present_false_when_other_info_codes(self):
        """Three-state semantics: when info codes exist but none is
        identity_optional, the flag must be False — not NULL. NULL is
        reserved for rows with no info codes at all (no denominator
        contribution). Without this distinction the C11 KPI denominator
        would conflate 'observed unsigned' with 'never observed'."""
        body = {
            "messages": [
                {"type": "info", "code": "tax_rounded_up"},
                {"type": "info", "code": "shipping_estimated"},
            ]
        }
        fields = UCPResponseParser.extract(body)
        # info codes observed → flag must land as a concrete BOOL.
        assert fields["identity_optional_present"] is False
        # And the codes themselves are still in the JSON column.
        codes = json.loads(fields["message_info_codes_json"])
        assert codes == ["tax_rounded_up", "shipping_estimated"]

    def test_identity_optional_flag_only_on_info_severity(self):
        """The convenience flag matches the info-code 'identity_optional'
        specifically; an error/warning code with the same string is a
        different signal and should NOT trip the flag."""
        body = {
            "messages": [
                {"type": "warning", "code": "identity_optional"},
                {"type": "error", "code": "identity_optional"},
            ]
        }
        fields = UCPResponseParser.extract(body)
        # info_codes empty → flag absent.
        assert "identity_optional_present" not in fields
        assert "message_info_codes_json" not in fields
        warnings = json.loads(fields["message_warning_codes_json"])
        assert warnings == ["identity_optional"]

    def test_extract_messages_only_errors_no_info_warning_columns(self):
        body = {
            "messages": [
                {
                    "type": "error",
                    "code": "missing_phone",
                    "content": "Phone is required",
                },
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["error_code"] == "missing_phone"
        assert "message_info_codes_json" not in fields
        assert "message_warning_codes_json" not in fields
        assert "identity_optional_present" not in fields

    def test_extract_messages_skips_malformed_codes(self):
        """Non-string or empty codes are dropped from the per-severity
        lists. A single bad sender shouldn't pollute the column with
        unfilterable values."""
        body = {
            "messages": [
                {"type": "info", "code": "valid_code"},
                {"type": "info", "code": ""},  # empty string
                {"type": "info", "code": None},  # null
                {"type": "info", "code": 42},  # non-string
                {"type": "info"},  # missing code
                {"type": "info", "code": "another_valid_code"},
            ]
        }
        fields = UCPResponseParser.extract(body)
        codes = json.loads(fields["message_info_codes_json"])
        assert codes == ["valid_code", "another_valid_code"]

    def test_extract_no_messages_no_columns(self):
        body = {"id": "chk_123", "status": "ready_for_complete"}
        fields = UCPResponseParser.extract(body)
        assert "message_info_codes_json" not in fields
        assert "message_warning_codes_json" not in fields
        assert "identity_optional_present" not in fields
        assert "messages_json" not in fields
        # No messages → no eligibility flag denominator, all three NULL.
        assert "eligibility_accepted_present" not in fields
        assert "eligibility_not_accepted_present" not in fields
        assert "eligibility_invalid_present" not in fields

    # --- A5: eligibility verification outcome (info + error severity) ---

    def test_eligibility_accepted_sets_accepted_true_others_false(self):
        """`eligibility_accepted` as info severity (the typical shape)
        sets accepted=TRUE and the other two FALSE — the trio is
        mutually exclusive in well-formed responses, so when one fires
        the dashboard can read FALSE for the other two as a concrete
        signal, not 'unknown'."""
        body = {
            "messages": [
                {
                    "type": "info",
                    "code": "eligibility_accepted",
                    "content": "Loyalty member discount applies",
                },
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["eligibility_accepted_present"] is True
        assert fields["eligibility_not_accepted_present"] is False
        assert fields["eligibility_invalid_present"] is False

    def test_eligibility_not_accepted_sets_only_that_flag_true(self):
        body = {
            "messages": [
                {
                    "type": "info",
                    "code": "eligibility_not_accepted",
                    "content": "Item not eligible for promo",
                },
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["eligibility_accepted_present"] is False
        assert fields["eligibility_not_accepted_present"] is True
        assert fields["eligibility_invalid_present"] is False

    def test_eligibility_invalid_walked_from_error_severity(self):
        """`eligibility_invalid` is canonically `error` severity in
        upstream's `error_code` enum, while the other two are
        typically `info`. The cross-severity walk must pick this code
        up from the error message — without that, every
        `eligibility_invalid` row would underpopulate the trio.
        Pin that the legacy error_code column also still gets
        populated from the same message."""
        body = {
            "messages": [
                {
                    "type": "error",
                    "code": "eligibility_invalid",
                    "content": "Eligibility claim malformed",
                    "severity": "recoverable",
                },
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["eligibility_accepted_present"] is False
        assert fields["eligibility_not_accepted_present"] is False
        assert fields["eligibility_invalid_present"] is True
        # Legacy first-error column still populated from the same msg.
        assert fields["error_code"] == "eligibility_invalid"

    def test_eligibility_outcome_codes_independent_of_other_codes(self):
        """A non-eligibility info code in the same response must NOT
        falsely populate the trio. The denominator is "an eligibility
        outcome code was observed", not "any info code was observed"
        — otherwise every checkout that ships `tax_rounded_up` would
        report eligibility_*_present = FALSE for all three, polluting
        the KPI."""
        body = {
            "messages": [
                {"type": "info", "code": "tax_rounded_up"},
                {"type": "info", "code": "identity_optional"},
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert "eligibility_accepted_present" not in fields
        assert "eligibility_not_accepted_present" not in fields
        assert "eligibility_invalid_present" not in fields

    def test_eligibility_no_codes_leaves_all_three_null(self):
        """No eligibility outcome code in messages → all three NULL,
        not FALSE. NULL is reserved for 'verification did not
        surface', which is a different signal from 'verification ran
        and a different outcome fired'."""
        body = {
            "messages": [
                {"type": "warning", "code": "shipping_delayed"},
            ],
            "context": {
                "eligibility": [{"claim": "loyalty_member"}],
            },
        }
        fields = UCPResponseParser.extract(body)
        # Even with context.eligibility authored, absence of an
        # outcome code keeps the trio NULL. The eligibility claim
        # payload lives in its own column.
        assert "eligibility_accepted_present" not in fields
        assert "eligibility_not_accepted_present" not in fields
        assert "eligibility_invalid_present" not in fields
        assert fields["context_eligibility_json"] == json.dumps(
            [{"claim": "loyalty_member"}]
        )

    def test_eligibility_duplicate_codes_collapse(self):
        """Set-based capture: duplicate codes don't change the
        outcome. Pin that a sender that emits the same eligibility
        code twice produces the same flag values as a single
        emission — no quadratic JSON growth, no flicker."""
        body = {
            "messages": [
                {"type": "info", "code": "eligibility_accepted"},
                {"type": "info", "code": "eligibility_accepted"},
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["eligibility_accepted_present"] is True
        assert fields["eligibility_not_accepted_present"] is False
        assert fields["eligibility_invalid_present"] is False

    def test_eligibility_two_codes_simultaneously_both_true(self):
        """The trio is mutually exclusive in well-formed responses,
        but if a malformed sender ships two outcome codes in the
        same row we record what they sent — both flags True, the
        third False. Analysts can detect the conflict via
        messages_json. This is the signal-fidelity guarantee:
        analytics records observed reality, not normalized reality."""
        body = {
            "messages": [
                {"type": "info", "code": "eligibility_accepted"},
                {
                    "type": "error",
                    "code": "eligibility_invalid",
                    "content": "Claim invalidated downstream",
                },
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["eligibility_accepted_present"] is True
        assert fields["eligibility_invalid_present"] is True
        assert fields["eligibility_not_accepted_present"] is False

    def test_eligibility_malformed_messages_skipped(self):
        """Non-string codes / missing code field don't crash and
        don't pollute the eligibility trio. A single bad sender
        shouldn't take down the row's other extracted fields."""
        body = {
            "messages": [
                {"type": "info", "code": None},
                {"type": "info", "code": 42},
                {"type": "info"},  # missing code
                "not-a-dict",  # malformed message entry
                {"type": "info", "code": "eligibility_accepted"},
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["eligibility_accepted_present"] is True
        assert fields["eligibility_not_accepted_present"] is False
        assert fields["eligibility_invalid_present"] is False

    def test_eligibility_alongside_unrelated_messages(self):
        """Real responses carry a mix of unrelated codes and possibly
        an eligibility outcome. Pin that the outcome trio coexists
        cleanly with the existing per-severity code lists and the
        first-error capture, exercising the single-pass loop."""
        body = {
            "messages": [
                {"type": "info", "code": "tax_rounded_up"},
                {"type": "warning", "code": "shipping_delayed"},
                {"type": "info", "code": "eligibility_not_accepted"},
                {
                    "type": "error",
                    "code": "missing_email",
                    "content": "Email required",
                },
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["eligibility_not_accepted_present"] is True
        assert fields["eligibility_accepted_present"] is False
        assert fields["eligibility_invalid_present"] is False
        # Existing columns remain unaffected.
        info = json.loads(fields["message_info_codes_json"])
        assert info == ["tax_rounded_up", "eligibility_not_accepted"]
        warnings = json.loads(fields["message_warning_codes_json"])
        assert warnings == ["shipping_delayed"]
        assert fields["error_code"] == "missing_email"

    def test_context_eligibility_queryable_via_existing_column(self):
        """The context.eligibility[] payload from C1 already lands in
        context_eligibility_json. A5 doesn't change that column — pin
        that the new outcome trio coexists with it, so dashboards can
        join on `JSON_QUERY(context_eligibility_json, '$[0].claim')`
        and `eligibility_accepted_present = TRUE` in the same query."""
        body = {
            "context": {
                "eligibility": [
                    {"claim": "loyalty_member", "tier": "gold"},
                ],
            },
            "messages": [
                {"type": "info", "code": "eligibility_accepted"},
            ],
        }
        fields = UCPResponseParser.extract(body)
        # JSON column queryable by JSON_QUERY downstream.
        eligibility = json.loads(fields["context_eligibility_json"])
        assert eligibility == [{"claim": "loyalty_member", "tier": "gold"}]
        # Outcome trio populated independently from the claim payload.
        assert fields["eligibility_accepted_present"] is True
        assert fields["eligibility_not_accepted_present"] is False
        assert fields["eligibility_invalid_present"] is False

    def test_extract_order_confirmation_in_checkout(self):
        """Spec: checkout.order is a nested object with id and permalink_url."""
        body = {
            "id": "chk_123",
            "status": "completed",
            "order": {
                "id": "order_abc",
                "permalink_url": "https://shop.example.com/orders/order_abc",
            },
        }
        fields = UCPResponseParser.extract(body)
        assert fields["order_id"] == "order_abc"
        assert fields["permalink_url"] == ("https://shop.example.com/orders/order_abc")

    def test_extract_order_object(self):
        """Order with checkout_id and permalink_url."""
        order = {
            "id": "order_xyz",
            "checkout_id": "chk_abc",
            "status": "shipped",
            "permalink_url": "https://shop.example.com/orders/order_xyz",
            "fulfillment": {
                "expectations": [
                    {
                        "method_type": "shipping",
                        "status": "shipped",
                        "destination": {
                            "address_country": "US",
                            "postal_code": "94043",
                        },
                        "line_items": [{"id": "li_1", "quantity": 1}],
                    },
                ],
            },
        }
        fields = UCPResponseParser.extract(order)
        assert fields["order_id"] == "order_xyz"
        assert fields["checkout_session_id"] == "chk_abc"
        assert fields["permalink_url"] == ("https://shop.example.com/orders/order_xyz")
        assert fields["fulfillment_type"] == "shipping"
        assert fields["fulfillment_destination_country"] == "US"

    def test_extract_totals_all_spec_types(self):
        """All 7 spec total types are extracted."""
        body = {
            "totals": [
                {"type": "items_discount", "amount": 200},
                {"type": "subtotal", "amount": 5000},
                {"type": "discount", "amount": 500},
                {"type": "fulfillment", "amount": 599},
                {"type": "tax", "amount": 400},
                {"type": "fee", "amount": 100},
                {"type": "total", "amount": 5399},
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["items_discount_amount"] == 200
        assert fields["subtotal_amount"] == 5000
        assert fields["discount_amount"] == 500
        assert fields["fulfillment_amount"] == 599
        assert fields["tax_amount"] == 400
        assert fields["fee_amount"] == 100
        assert fields["total_amount"] == 5399

    def test_extract_continue_url(self):
        """continue_url for escalation."""
        body = {
            "status": "requires_escalation",
            "continue_url": "https://shop.example.com/checkout/escalate",
        }
        fields = UCPResponseParser.extract(body)
        assert fields["continue_url"] == ("https://shop.example.com/checkout/escalate")

    # --- B1 / C9: totals SUM aggregation + totals_json ---

    def test_totals_duplicate_types_sum_instead_of_last_wins(self):
        """`total.json` permits multiple detail rows of the same
        well-known type (split state+local tax, multi-line discount,
        etc.). Scalar amount columns must SUM, not last-wins.
        Reviewer's PR-audit repro: tax 100 + 25 -> tax_amount 125."""
        body = {
            "totals": [
                {"type": "tax", "amount": 100, "display_text": "state tax"},
                {"type": "tax", "amount": 25, "display_text": "local tax"},
                {"type": "discount", "amount": -200},
                {"type": "discount", "amount": -50},
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["tax_amount"] == 125
        assert fields["discount_amount"] == -250

    def test_totals_signed_amounts_round_trip(self):
        """`signed_amount.json` permits negative integer amounts
        (refunds, credits). The SUM keeps signedness so a refund
        row's totals stay negative end-to-end."""
        body = {
            "totals": [
                {"type": "total", "amount": -1999, "display_text": "refund"},
            ]
        }
        fields = UCPResponseParser.extract(body)
        assert fields["total_amount"] == -1999

    def test_totals_json_preserves_full_ordered_array(self):
        """The verbatim totals array — including duplicates, custom
        `display_text`, `lines[]` itemization, and business-defined
        types — lands in `totals_json`. Scalar columns drop
        business-defined types (open vocab beyond the seven
        well-known ones); `totals_json` keeps them so dashboards
        can pivot on per-business categories."""
        totals = [
            {"type": "subtotal", "amount": 5000, "lines": [{"sku": "A"}]},
            {"type": "tax", "amount": 100, "display_text": "state"},
            {"type": "tax", "amount": 25, "display_text": "local"},
            {"type": "dev.merchant.custom_fee", "amount": 50},
            {"type": "total", "amount": 5175},
        ]
        body = {"totals": totals}
        fields = UCPResponseParser.extract(body)
        round_tripped = json.loads(fields["totals_json"])
        assert round_tripped == totals
        # Scalar SUM still works on well-known types.
        assert fields["tax_amount"] == 125
        # Business-defined type doesn't appear as a scalar column
        # but is preserved in totals_json above.
        assert "dev.merchant.custom_fee_amount" not in fields

    def test_totals_json_filters_non_dict_entries(self):
        body = {
            "totals": [
                {"type": "tax", "amount": 100},
                "not-a-dict",
                None,
                {"type": "total", "amount": 100},
            ]
        }
        fields = UCPResponseParser.extract(body)
        round_tripped = json.loads(fields["totals_json"])
        # Non-dict entries dropped from the JSON column.
        assert len(round_tripped) == 2
        # Scalar SUM still works.
        assert fields["tax_amount"] == 100
        assert fields["total_amount"] == 100

    def test_totals_non_int_amount_dropped(self):
        """Amounts must be integers per `signed_amount.json`. A
        string / float / bool amount is out-of-spec; the entry is
        dropped from SUM but the totals_json still preserves it
        verbatim (signal fidelity)."""
        body = {
            "totals": [
                {"type": "tax", "amount": "100"},  # string
                {"type": "tax", "amount": 99.5},  # float
                {"type": "tax", "amount": True},  # bool (int subclass)
                {"type": "tax", "amount": 50},  # well-formed
            ]
        }
        fields = UCPResponseParser.extract(body)
        # Only the int entry counts toward SUM.
        assert fields["tax_amount"] == 50

    def test_totals_empty_array_omits_json_column(self):
        body = {"totals": []}
        fields = UCPResponseParser.extract(body)
        assert "totals_json" not in fields
        assert "tax_amount" not in fields

    def test_totals_no_totals_key_omits_json_column(self):
        body = {"id": "chk_123"}
        fields = UCPResponseParser.extract(body)
        assert "totals_json" not in fields

    # --- C7: order_label ---

    def test_order_label_extracted_on_order_body(self):
        """`label` per `order.json` (PR #326). Business-set,
        order-shaped (carries checkout_id) only."""
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "label": "ORD-2026-00042",
        }
        fields = UCPResponseParser.extract(body)
        assert fields["order_label"] == "ORD-2026-00042"
        # And order_id correctly classified.
        assert fields["order_id"] == "order_xyz"

    def test_order_label_not_extracted_on_checkout_body(self):
        """A stray `label` on a checkout body (no checkout_id, since
        it IS the checkout) must NOT misattribute to order_label."""
        body = {
            "id": "chk_123",
            "status": "ready_for_complete",
            "label": "should-not-leak-to-order-label",
        }
        fields = UCPResponseParser.extract(body)
        assert "order_label" not in fields
        assert fields["checkout_session_id"] == "chk_123"

    def test_order_label_absent_when_not_in_body(self):
        body = {"id": "order_xyz", "checkout_id": "chk_a"}
        fields = UCPResponseParser.extract(body)
        assert "order_label" not in fields

    def test_order_label_non_string_dropped(self):
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "label": 42,  # malformed sender
        }
        fields = UCPResponseParser.extract(body)
        assert "order_label" not in fields

    def test_order_label_empty_string_dropped(self):
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "label": "",
        }
        fields = UCPResponseParser.extract(body)
        assert "order_label" not in fields

    # --- B5: ORDER_GET ---

    def test_get_orders_with_no_lifecycle_classifies_as_order_get(self):
        """GET /orders/{id} returning a no-lifecycle order body
        classifies as ORDER_GET (read-only poll), distinct from
        ORDER_UPDATED (REST PUT mutation)."""
        result = UCPResponseParser.classify(
            "GET",
            "/orders/order_xyz",
            200,
            response_body={"id": "order_xyz", "checkout_id": "chk_a"},
        )
        assert result == UCPEventType.ORDER_GET

    def test_get_orders_with_lifecycle_classifies_by_lifecycle(self):
        """A GET returning a `delivered` fulfillment event still
        classifies as ORDER_DELIVERED — lifecycle wins on either
        GET or PUT."""
        result = UCPResponseParser.classify(
            "GET",
            "/orders/order_xyz",
            200,
            response_body={
                "id": "order_xyz",
                "checkout_id": "chk_a",
                "fulfillment": {
                    "events": [
                        {
                            "id": "fe_1",
                            "occurred_at": "2026-05-09T17:00:00Z",
                            "type": "delivered",
                            "line_items": [],
                        },
                    ]
                },
            },
        )
        assert result == UCPEventType.ORDER_DELIVERED

    def test_put_orders_still_classifies_as_order_updated(self):
        """A REST PUT remains ORDER_UPDATED — only GET changes
        behavior. ORDER_UPDATED is the mutation path; ORDER_GET is
        the read-only poll."""
        result = UCPResponseParser.classify(
            "PUT",
            "/orders/order_xyz",
            200,
            response_body={"id": "order_xyz", "checkout_id": "chk_a"},
        )
        assert result == UCPEventType.ORDER_UPDATED

    def test_extract_identity_fields(self):
        """Identity provider and scope from response body."""
        body = {"provider": "google", "scope": "openid email"}
        fields = UCPResponseParser.extract(body)
        assert fields["identity_provider"] == "google"
        assert fields["identity_scope"] == "openid email"

    def test_extract_identity_nested(self):
        """Identity fields from nested identity object."""
        body = {"identity": {"provider": "github", "scope": "read:user"}}
        fields = UCPResponseParser.extract(body)
        assert fields["identity_provider"] == "github"
        assert fields["identity_scope"] == "read:user"

    def test_extract_empty(self):
        assert UCPResponseParser.extract(None) == {}
        assert UCPResponseParser.extract({}) == {}


class TestClassifyJsonRPC:
    """Tests for classify_jsonrpc() — MCP and A2A tool name mapping."""

    def test_mcp_create_checkout(self):
        assert (
            UCPResponseParser.classify_jsonrpc("create_checkout")
            == UCPEventType.CHECKOUT_SESSION_CREATED
        )

    def test_mcp_complete_checkout(self):
        assert (
            UCPResponseParser.classify_jsonrpc("complete_checkout")
            == UCPEventType.CHECKOUT_SESSION_COMPLETED
        )

    def test_mcp_cancel_checkout(self):
        assert (
            UCPResponseParser.classify_jsonrpc("cancel_checkout")
            == UCPEventType.CHECKOUT_SESSION_CANCELED
        )

    def test_mcp_discover(self):
        assert (
            UCPResponseParser.classify_jsonrpc("discover_merchant")
            == UCPEventType.PROFILE_DISCOVERED
        )

    def test_mcp_create_cart(self):
        assert (
            UCPResponseParser.classify_jsonrpc("create_cart")
            == UCPEventType.CART_CREATED
        )

    def test_mcp_catalog_search(self):
        assert (
            UCPResponseParser.classify_jsonrpc("catalog_search")
            == UCPEventType.CATALOG_SEARCH
        )

    def test_mcp_catalog_lookup(self):
        assert (
            UCPResponseParser.classify_jsonrpc("catalog_lookup")
            == UCPEventType.CATALOG_LOOKUP
        )

    def test_mcp_catalog_product_get(self):
        assert (
            UCPResponseParser.classify_jsonrpc("get_product")
            == UCPEventType.CATALOG_PRODUCT_GET
        )

    def test_a2a_catalog_search(self):
        assert (
            UCPResponseParser.classify_jsonrpc("a2a.ucp.catalog.search")
            == UCPEventType.CATALOG_SEARCH
        )

    def test_mcp_create_order(self):
        assert (
            UCPResponseParser.classify_jsonrpc("create_order")
            == UCPEventType.ORDER_CREATED
        )

    def test_mcp_get_order_delivered(self):
        assert (
            UCPResponseParser.classify_jsonrpc(
                "get_order", 200, {"status": "delivered"}
            )
            == UCPEventType.ORDER_DELIVERED
        )

    def test_mcp_update_order_classifies_as_order_updated(self):
        """B5 / ADK-parity regression. The `update_order` MCP tool
        maps to `PUT /orders/{id}` and a no-lifecycle body falls
        through the lifecycle helper to ORDER_UPDATED. Without the
        parser._TOOL_TO_HTTP entry, the same tool emitted REQUEST
        through `record_jsonrpc` while the ADK plugin emitted
        ORDER_UPDATED — a KPI-divergence trap across transports."""
        assert (
            UCPResponseParser.classify_jsonrpc(
                "update_order",
                200,
                {"id": "order_xyz", "checkout_id": "chk_a", "status": "confirmed"},
            )
            == UCPEventType.ORDER_UPDATED
        )

    def test_a2a_order_update_classifies_as_order_updated(self):
        """Symmetric A2A action `a2a.ucp.order.update` lands on the
        same mapping. Pin so the two MCP/A2A spellings can't drift
        independently."""
        assert (
            UCPResponseParser.classify_jsonrpc(
                "a2a.ucp.order.update",
                200,
                {"id": "order_xyz", "checkout_id": "chk_a", "status": "confirmed"},
            )
            == UCPEventType.ORDER_UPDATED
        )

    def test_a2a_checkout_create(self):
        assert (
            UCPResponseParser.classify_jsonrpc("a2a.ucp.checkout.create")
            == UCPEventType.CHECKOUT_SESSION_CREATED
        )

    def test_a2a_checkout_complete(self):
        assert (
            UCPResponseParser.classify_jsonrpc("a2a.ucp.checkout.complete")
            == UCPEventType.CHECKOUT_SESSION_COMPLETED
        )

    def test_a2a_identity_link(self):
        assert (
            UCPResponseParser.classify_jsonrpc("a2a.ucp.identity.link")
            == UCPEventType.IDENTITY_LINK_INITIATED
        )

    def test_a2a_identity_revoke(self):
        assert (
            UCPResponseParser.classify_jsonrpc("a2a.ucp.identity.revoke")
            == UCPEventType.IDENTITY_LINK_REVOKED
        )

    def test_negotiate_capability(self):
        assert (
            UCPResponseParser.classify_jsonrpc("negotiate_capability")
            == UCPEventType.CAPABILITY_NEGOTIATED
        )

    def test_a2a_capability_negotiate(self):
        assert (
            UCPResponseParser.classify_jsonrpc("a2a.ucp.capability.negotiate")
            == UCPEventType.CAPABILITY_NEGOTIATED
        )

    def test_add_to_checkout(self):
        assert (
            UCPResponseParser.classify_jsonrpc("add_to_checkout")
            == UCPEventType.CHECKOUT_SESSION_UPDATED
        )

    def test_remove_from_checkout(self):
        assert (
            UCPResponseParser.classify_jsonrpc("remove_from_checkout")
            == UCPEventType.CHECKOUT_SESSION_UPDATED
        )

    def test_start_payment(self):
        assert (
            UCPResponseParser.classify_jsonrpc("start_payment")
            == UCPEventType.CHECKOUT_SESSION_UPDATED
        )

    def test_update_customer_details(self):
        assert (
            UCPResponseParser.classify_jsonrpc("update_customer_details")
            == UCPEventType.CHECKOUT_SESSION_UPDATED
        )

    def test_unknown_tool(self):
        assert UCPResponseParser.classify_jsonrpc("get_weather") == UCPEventType.REQUEST


class TestWebhookClassification:
    """Tests for upstream partner webhook path classification."""

    def test_partner_webhook_shipped_via_request_body(self):
        """Upstream: order payload is in request_body, response is ack."""
        order = {"id": "order_1", "checkout_id": "chk_1", "status": "shipped"}
        assert (
            UCPResponseParser.classify(
                "POST",
                "/webhooks/partners/p1/events/order",
                200,
                {"status": "ok"},
                request_body=order,
            )
            == UCPEventType.ORDER_SHIPPED
        )

    def test_partner_webhook_delivered_via_request_body(self):
        order = {"id": "order_1", "checkout_id": "chk_1", "status": "delivered"}
        assert (
            UCPResponseParser.classify(
                "POST",
                "/webhooks/partners/p1/events/order",
                200,
                {"status": "ok"},
                request_body=order,
            )
            == UCPEventType.ORDER_DELIVERED
        )

    def test_partner_webhook_returned(self):
        assert (
            UCPResponseParser.classify(
                "POST",
                "/webhooks/partners/p1/events/order",
                200,
                {"status": "returned"},
            )
            == UCPEventType.ORDER_RETURNED
        )

    def test_partner_webhook_canceled(self):
        assert (
            UCPResponseParser.classify(
                "POST",
                "/webhooks/partners/p1/events/order",
                200,
                {"status": "canceled"},
            )
            == UCPEventType.ORDER_CANCELED
        )

    def test_partner_webhook_cancelled_british(self):
        assert (
            UCPResponseParser.classify(
                "POST",
                "/webhooks/partners/p1/events/order",
                200,
                {"status": "cancelled"},
            )
            == UCPEventType.ORDER_CANCELED
        )

    def test_partner_webhook_no_body(self):
        # Webhook detected by path, body has no recognizable lifecycle
        # status → ORDER_WEBHOOK_RECEIVED (B5b: don't pivot taxonomy on
        # URL format). Distinct from ORDER_UPDATED, which is reserved
        # for REST-driven /orders/{id} updates from the business side.
        assert (
            UCPResponseParser.classify(
                "POST",
                "/webhooks/partners/p1/events/order",
                200,
                None,
            )
            == UCPEventType.ORDER_WEBHOOK_RECEIVED
        )

    def test_generic_webhook_fallback(self):
        assert (
            UCPResponseParser.classify("POST", "/webhooks/some-other-event", 200, {})
            == UCPEventType.ORDER_WEBHOOK_RECEIVED
        )

    def test_webhook_error_500(self):
        """Webhook 5xx should classify as error, not order_updated."""
        assert (
            UCPResponseParser.classify(
                "POST", "/webhooks/partners/p1/events/order", 500, {}
            )
            == UCPEventType.ERROR
        )

    def test_webhook_error_400(self):
        """Webhook 4xx should classify as error."""
        assert (
            UCPResponseParser.classify("POST", "/webhooks/some-event", 400, {})
            == UCPEventType.ERROR
        )

    # ---- B5b: platform-provided webhook URLs + header fallback ----

    def test_platform_provided_webhook_url_via_extra_prefix(self):
        """UCP order.md: 'The URL format is platform-specific.' A
        platform that publishes its webhook destination as `/events`
        is the canonical motivation for this row -- the default
        `/webhook(s)` filter alone misses it. The classifier must
        accept operator-configured prefixes via webhook_path_prefixes
        and route the request through the order-webhook branch."""
        result = UCPResponseParser.classify(
            "POST",
            "/events",
            200,
            response_body=None,
            request_body={"id": "order_xyz", "status": "shipped"},
            webhook_path_prefixes=("/events",),
        )
        assert result == UCPEventType.ORDER_SHIPPED

    def test_header_based_webhook_fallback_unknown_path(self):
        """When the URL is not a known UCP path, presence of
        Webhook-Id + Webhook-Timestamp on the request must trigger
        the order-webhook branch -- this is the safety net for
        platforms whose webhook URL the operator hasn't enumerated.
        UCP order.md requires both headers on every order webhook,
        so the pair is a strong fingerprint."""
        result = UCPResponseParser.classify(
            "POST",
            "/hooks/abc-123",
            200,
            response_body={"status": "ok"},
            request_body={"id": "order_xyz", "status": "delivered"},
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )
        assert result == UCPEventType.ORDER_DELIVERED

    def test_header_fallback_suppressed_on_known_rest_path(self):
        """Webhook headers on /checkout-sessions can only come from a
        buggy or malicious sender -- the URL determines the operation
        on known UCP REST endpoints. The classifier must NOT route
        such requests into the webhook branch even though the headers
        are present."""
        result = UCPResponseParser.classify(
            "POST",
            "/checkout-sessions",
            201,
            response_body={"id": "chk_xyz", "status": "ready_for_complete"},
            request_headers={
                "Webhook-Id": "evt_definitely_not_a_webhook",
                "Webhook-Timestamp": "1767225600",
            },
        )
        # /checkout-sessions POST → CHECKOUT_SESSION_CREATED, not any
        # ORDER_* type.
        assert result == UCPEventType.CHECKOUT_SESSION_CREATED

    def test_header_only_one_of_pair_does_not_trigger(self):
        """Standard Webhooks ships Webhook-Id AND Webhook-Timestamp
        together; either alone is not a valid delivery. A request
        with only one of the pair must not trigger the header
        fallback -- otherwise senders that happen to use a similarly
        named header for unrelated purposes would be misdetected."""
        result_id_only = UCPResponseParser.classify(
            "POST",
            "/api/v1/random",
            200,
            response_body={},
            request_headers={"Webhook-Id": "evt_42"},
        )
        result_ts_only = UCPResponseParser.classify(
            "POST",
            "/api/v1/random",
            200,
            response_body={},
            request_headers={"Webhook-Timestamp": "1767225600"},
        )
        # Falls through past the webhook branch — the path doesn't
        # match any UCP marker either, so we land on the generic
        # REQUEST fallback rather than ORDER_*.
        assert result_id_only != UCPEventType.ORDER_WEBHOOK_RECEIVED
        assert result_ts_only != UCPEventType.ORDER_WEBHOOK_RECEIVED

    def test_header_fallback_with_body_lifecycle_status(self):
        """The webhook detection (header pair) and the lifecycle
        derivation (body status) are independent: detection enters
        the branch, body picks the specific event type. Pin that
        body status drives taxonomy regardless of which signal got
        us into the branch."""
        for status, expected in [
            ("shipped", UCPEventType.ORDER_SHIPPED),
            ("delivered", UCPEventType.ORDER_DELIVERED),
            ("returned", UCPEventType.ORDER_RETURNED),
            ("canceled", UCPEventType.ORDER_CANCELED),
            ("cancelled", UCPEventType.ORDER_CANCELED),
        ]:
            result = UCPResponseParser.classify(
                "POST",
                "/ucp-events/incoming",
                200,
                response_body={"status": "ok"},
                request_body={"status": status},
                request_headers={
                    "Webhook-Id": f"evt_{status}",
                    "Webhook-Timestamp": "1767225600",
                },
            )
            assert result == expected, f"status={status}"

    def test_header_fallback_no_lifecycle_status_emits_webhook_received(self):
        """A webhook detected by headers but with no body lifecycle
        status emits ORDER_WEBHOOK_RECEIVED. This is distinct from
        ORDER_UPDATED (REST-driven business->platform updates) and
        the new B5b taxonomy: the URL no longer determines the
        type, the body does, and absence-of-status has its own
        first-class type."""
        result = UCPResponseParser.classify(
            "POST",
            "/hooks/123",
            200,
            response_body={"status": "ok"},
            request_body={"id": "order_xyz"},
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )
        assert result == UCPEventType.ORDER_WEBHOOK_RECEIVED

    def test_legacy_url_segment_fallback_still_works(self):
        """Senders that don't include status in body but use the
        legacy URL-segment convention (`/webhooks/order-delivered`)
        still classify correctly. Body-driven derivation takes
        precedence; URL segment is the fallback for body-less
        senders."""
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/order-delivered",
            200,
            response_body=None,
            request_body=None,
        )
        assert result == UCPEventType.ORDER_DELIVERED


class TestOrderLifecycleNewShape:
    """B8 — at UCP order.md/c5c6139 the order has no top-level `status`.
    Lifecycle moves into `fulfillment.events[]` (append-only shipment
    log) and `adjustments[]` (post-order refunds / returns / disputes
    / cancellations). The classifier must derive ORDER_* event types
    from these arrays, not from the now-absent flat field."""

    def _new_shape_order(self, fulfillment_events=None, adjustments=None):
        """Build a c5c6139-shaped order body."""
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "line_items": [],
            "fulfillment": {"events": fulfillment_events or []},
        }
        if adjustments is not None:
            body["adjustments"] = adjustments
        return body

    def test_webhook_classifies_shipped_from_fulfillment_event(self):
        """The minimum-viable case: a webhook ships a new-shape order
        body with a single `shipped` fulfillment event. The previous
        body.get('status') path returned no status → ORDER_WEBHOOK_-
        RECEIVED, masking the lifecycle. B8 must turn that into
        ORDER_SHIPPED."""
        body = self._new_shape_order(
            fulfillment_events=[
                {
                    "id": "fe_1",
                    "occurred_at": "2026-05-09T12:00:00Z",
                    "type": "shipped",
                    "line_items": [{"id": "li_1", "quantity": 1}],
                    "tracking_number": "1Z999",
                },
            ]
        )
        result = UCPResponseParser.classify(
            "POST",
            "/hooks/abc",
            200,
            response_body={"status": "ok"},
            request_body=body,
            request_headers={
                "Webhook-Id": "evt_42",
                "Webhook-Timestamp": "1767225600",
            },
        )
        assert result == UCPEventType.ORDER_SHIPPED

    def test_webhook_classifies_delivered_from_latest_fulfillment_event(self):
        """Multiple events ordered by timestamp: the latest event's
        type wins. A `shipped` event followed by a `delivered` event
        must classify as ORDER_DELIVERED — the current shipment state
        is what dashboards pivot on."""
        body = self._new_shape_order(
            fulfillment_events=[
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
            ]
        )
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_DELIVERED

    def test_webhook_classifies_out_of_order_events_by_occurred_at(self):
        """Append-only contract says array order is chronological, but
        a sender that batches / re-sends late deliveries shouldn't
        flip lifecycle to the wrong state. The latest_by_occurred_at
        helper sorts so a delivered event that lands earlier in the
        array than a shipped event still wins (it has the higher
        timestamp)."""
        body = self._new_shape_order(
            fulfillment_events=[
                # delivered occurred LATER but ships earlier in the
                # array — defensive sort by occurred_at picks it.
                {
                    "id": "fe_2",
                    "occurred_at": "2026-05-09T17:00:00Z",
                    "type": "delivered",
                    "line_items": [],
                },
                {
                    "id": "fe_1",
                    "occurred_at": "2026-05-08T08:00:00Z",
                    "type": "shipped",
                    "line_items": [],
                },
            ]
        )
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_DELIVERED

    def test_returned_to_sender_classifies_as_returned(self):
        body = self._new_shape_order(
            fulfillment_events=[
                {
                    "id": "fe_1",
                    "occurred_at": "2026-05-09T17:00:00Z",
                    "type": "returned_to_sender",
                    "line_items": [],
                },
            ]
        )
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_RETURNED

    def test_undeliverable_and_canceled_classify_as_canceled(self):
        for event_type in ("canceled", "cancelled", "undeliverable"):
            body = self._new_shape_order(
                fulfillment_events=[
                    {
                        "id": "fe_1",
                        "occurred_at": "2026-05-09T17:00:00Z",
                        "type": event_type,
                        "line_items": [],
                    },
                ]
            )
            result = UCPResponseParser.classify(
                "POST",
                "/webhooks/orders",
                200,
                response_body={"status": "ok"},
                request_body=body,
            )
            assert result == UCPEventType.ORDER_CANCELED, f"type={event_type}"

    def test_in_transit_classifies_as_shipped(self):
        body = self._new_shape_order(
            fulfillment_events=[
                {
                    "id": "fe_1",
                    "occurred_at": "2026-05-09T17:00:00Z",
                    "type": "in_transit",
                    "line_items": [],
                },
            ]
        )
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        # in_transit is a substate of "the package is on its way";
        # surfaces as ORDER_SHIPPED for the lifecycle bucket.
        assert result == UCPEventType.ORDER_SHIPPED

    def test_adjustment_refund_classifies_as_returned_when_no_fulfillment(self):
        """A refund-only webhook (no fulfillment events) maps to
        ORDER_RETURNED. Adjustments are independent post-order events;
        a refund is the money-side counterpart to a physical return."""
        body = self._new_shape_order(
            fulfillment_events=[],
            adjustments=[
                {
                    "id": "adj_1",
                    "type": "refund",
                    "occurred_at": "2026-05-09T20:00:00Z",
                    "status": "completed",
                },
            ],
        )
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_RETURNED

    def test_adjustment_cancellation_classifies_as_canceled(self):
        body = self._new_shape_order(
            fulfillment_events=[],
            adjustments=[
                {
                    "id": "adj_1",
                    "type": "cancellation",
                    "occurred_at": "2026-05-09T20:00:00Z",
                    "status": "completed",
                },
            ],
        )
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_CANCELED

    def test_fulfillment_events_take_precedence_over_adjustments(self):
        """When both arrays carry lifecycle-bearing entries,
        `fulfillment.events[]` wins — it's the authoritative shipment
        log. An adjustment.type='refund' alongside a
        fulfillment_event.type='delivered' classifies as
        ORDER_DELIVERED (current shipment state), not ORDER_RETURNED
        (the refund is a post-order money event)."""
        body = self._new_shape_order(
            fulfillment_events=[
                {
                    "id": "fe_1",
                    "occurred_at": "2026-05-09T12:00:00Z",
                    "type": "delivered",
                    "line_items": [],
                },
            ],
            adjustments=[
                {
                    "id": "adj_1",
                    "type": "refund",
                    "occurred_at": "2026-05-09T20:00:00Z",  # later
                    "status": "completed",
                },
            ],
        )
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_DELIVERED

    def test_legacy_top_level_status_still_classifies(self):
        """Senders that haven't moved to c5c6139 still ship the flat
        shape. The legacy fallback must keep working — pinned with a
        no-fulfillment.events/no-adjustments body carrying only
        top-level `status`."""
        body = {"id": "order_x", "checkout_id": "chk_a", "status": "delivered"}
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_DELIVERED

    def test_new_shape_overrides_legacy_status_when_both_present(self):
        """If a transitional sender ships BOTH a top-level status and
        new-shape fulfillment.events[], new-shape wins. This is the
        correct precedence as the new shape is the documented current
        spec; the legacy field is the fallback for pre-migration
        senders only."""
        body = self._new_shape_order(
            fulfillment_events=[
                {
                    "id": "fe_1",
                    "occurred_at": "2026-05-09T17:00:00Z",
                    "type": "delivered",
                    "line_items": [],
                },
            ],
        )
        body["status"] = "shipped"  # legacy conflicting signal
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_DELIVERED

    def test_empty_fulfillment_events_falls_through_to_webhook_received(self):
        """A new-shape body with an empty events array and no
        adjustments/status carries no lifecycle information →
        ORDER_WEBHOOK_RECEIVED (the B5b generic-receipt type).
        Empty != missing: the merchant explicitly told us there's no
        shipment state yet."""
        body = self._new_shape_order(fulfillment_events=[])
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_WEBHOOK_RECEIVED

    def test_unknown_fulfillment_event_type_falls_through(self):
        """`type` is an open string per the schema; we map only the
        documented common values. A custom merchant-specific type
        falls through to ORDER_WEBHOOK_RECEIVED rather than picking
        a wrong lifecycle bucket — dashboards can still pivot on
        latest_fulfillment_event_type for the custom name."""
        body = self._new_shape_order(
            fulfillment_events=[
                {
                    "id": "fe_1",
                    "occurred_at": "2026-05-09T17:00:00Z",
                    "type": "merchant_custom_warehouse_pickup",
                    "line_items": [],
                },
            ],
        )
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_WEBHOOK_RECEIVED

    def test_rest_orders_get_classifies_from_fulfillment_events(self):
        """The /orders/{id} REST GET path must derive lifecycle from
        the same arrays — not just the webhook branch. Dashboards
        that join webhook traffic and REST polling traffic need the
        same taxonomy on both."""
        body = self._new_shape_order(
            fulfillment_events=[
                {
                    "id": "fe_1",
                    "occurred_at": "2026-05-09T17:00:00Z",
                    "type": "delivered",
                    "line_items": [],
                },
            ],
        )
        result = UCPResponseParser.classify(
            "GET",
            "/orders/order_xyz",
            200,
            response_body=body,
        )
        assert result == UCPEventType.ORDER_DELIVERED

    def test_malformed_entries_do_not_crash_classifier(self):
        """A single non-dict entry in fulfillment.events[] (or
        adjustments[]) must not take down the row. We skip it and
        derive lifecycle from the remaining well-formed entries."""
        body = self._new_shape_order(
            fulfillment_events=[
                "not-a-dict",
                None,
                {
                    "id": "fe_1",
                    "occurred_at": "2026-05-09T17:00:00Z",
                    "type": "delivered",
                    "line_items": [],
                },
            ],
        )
        result = UCPResponseParser.classify(
            "POST",
            "/webhooks/orders",
            200,
            response_body={"status": "ok"},
            request_body=body,
        )
        assert result == UCPEventType.ORDER_DELIVERED


class TestOrderLifecycleExtraction:
    """B8 — extract-side companion to TestOrderLifecycleNewShape.
    The JSON arrays and latest_* scalars feed dashboards that need
    multi-event detail or fast pivots on the current shipment /
    adjustment state."""

    def test_fulfillment_events_serialized_to_json_column(self):
        events = [
            {
                "id": "fe_1",
                "occurred_at": "2026-05-08T08:00:00Z",
                "type": "shipped",
                "line_items": [{"id": "li_1", "quantity": 1}],
                "tracking_number": "1Z999",
                "carrier": "UPS",
            },
            {
                "id": "fe_2",
                "occurred_at": "2026-05-09T17:00:00Z",
                "type": "delivered",
                "line_items": [{"id": "li_1", "quantity": 1}],
            },
        ]
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "fulfillment": {"events": events},
        }
        fields = UCPResponseParser.extract(body)
        # Full array round-trips through JSON for downstream
        # JSON_QUERY_ARRAY access.
        serialized = json.loads(fields["fulfillment_events_json"])
        assert serialized == events
        # Latest scalars picked by occurred_at — `delivered` is the
        # later event, so it surfaces.
        assert fields["latest_fulfillment_event_type"] == "delivered"
        assert fields["latest_fulfillment_event_at"] == "2026-05-09T17:00:00Z"

    def test_adjustments_serialized_to_json_column(self):
        adjustments = [
            {
                "id": "adj_1",
                "type": "refund",
                "occurred_at": "2026-05-09T20:00:00Z",
                "status": "completed",
                "description": "Defective item",
            },
        ]
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "adjustments": adjustments,
        }
        fields = UCPResponseParser.extract(body)
        serialized = json.loads(fields["adjustments_json"])
        assert serialized == adjustments
        assert fields["latest_adjustment_type"] == "refund"
        assert fields["latest_adjustment_status"] == "completed"
        assert fields["latest_adjustment_at"] == "2026-05-09T20:00:00Z"

    def test_empty_arrays_omit_columns(self):
        """No entries → no columns. Three-state nullable semantics:
        NULL is "no data observed", distinct from FALSE / 0 / empty
        string. Dashboards that COUNT(latest_*) get the right
        "had any lifecycle data" denominator."""
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "fulfillment": {"events": []},
            "adjustments": [],
        }
        fields = UCPResponseParser.extract(body)
        assert "fulfillment_events_json" not in fields
        assert "adjustments_json" not in fields
        assert "latest_fulfillment_event_type" not in fields
        assert "latest_adjustment_type" not in fields

    def test_malformed_entries_filtered_from_json_column(self):
        """Non-dict entries are skipped from the serialized JSON so
        downstream JSON_QUERY doesn't choke. The clean entries
        survive, including for latest_* derivation."""
        events = [
            "not-a-dict",
            None,
            42,
            {
                "id": "fe_1",
                "occurred_at": "2026-05-09T17:00:00Z",
                "type": "delivered",
                "line_items": [],
            },
        ]
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "fulfillment": {"events": events},
        }
        fields = UCPResponseParser.extract(body)
        serialized = json.loads(fields["fulfillment_events_json"])
        # Only the clean dict survives.
        assert len(serialized) == 1
        assert serialized[0]["id"] == "fe_1"
        assert fields["latest_fulfillment_event_type"] == "delivered"

    def test_no_fulfillment_object_at_all(self):
        body = {"id": "order_xyz", "checkout_id": "chk_a"}
        fields = UCPResponseParser.extract(body)
        assert "fulfillment_events_json" not in fields
        assert "latest_fulfillment_event_type" not in fields

    def test_fulfillment_events_with_only_one_event(self):
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "fulfillment": {
                "events": [
                    {
                        "id": "fe_1",
                        "occurred_at": "2026-05-09T17:00:00Z",
                        "type": "processing",
                        "line_items": [],
                    },
                ]
            },
        }
        fields = UCPResponseParser.extract(body)
        assert fields["latest_fulfillment_event_type"] == "processing"
        # `processing` doesn't map to any ORDER_* lifecycle event,
        # but the extract-side column still records it for
        # dashboards that pivot on the raw type.

    def test_latest_picked_by_occurred_at_not_array_position(self):
        """Out-of-order array — analytics records the entry with
        the latest occurred_at, not the last array element. Defensive
        against senders that batch deliveries."""
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_a",
            "fulfillment": {
                "events": [
                    {
                        "id": "fe_late",
                        "occurred_at": "2026-05-09T17:00:00Z",
                        "type": "delivered",
                        "line_items": [],
                    },
                    {
                        "id": "fe_early",
                        "occurred_at": "2026-05-08T08:00:00Z",
                        "type": "shipped",
                        "line_items": [],
                    },
                ]
            },
        }
        fields = UCPResponseParser.extract(body)
        assert fields["latest_fulfillment_event_type"] == "delivered"
        assert fields["latest_fulfillment_event_at"] == "2026-05-09T17:00:00Z"

    def test_mixed_z_and_offset_timestamps_compared_as_instants(self):
        """RFC 3339 lets timestamps use Z (UTC) or numeric offsets like
        +02:00. Lexicographic sort on the raw strings would compare
        "12:00:00+02:00" > "10:30:00Z" even though +02:00 makes 12:00
        local equal to 10:00 UTC — earlier than 10:30 UTC. Parse to
        aware datetimes for comparison so the actual instant wins.

        Reviewer's PR #20 repro: a `shipped` event with offset
        timestamp paired with a `delivered` event in Z form. The
        delivered instant (10:30 UTC) is later than the shipped
        instant (10:00 UTC), so the latest-event scalar must be
        `delivered` and the lifecycle event must be ORDER_DELIVERED."""
        body = {
            "id": "order_1",
            "checkout_id": "chk_1",
            "fulfillment": {
                "events": [
                    {
                        "id": "fe_1",
                        "occurred_at": "2026-05-09T10:30:00Z",  # 10:30 UTC
                        "type": "delivered",
                        "line_items": [],
                    },
                    {
                        "id": "fe_2",
                        "occurred_at": "2026-05-09T12:00:00+02:00",  # 10:00 UTC
                        "type": "shipped",
                        "line_items": [],
                    },
                ]
            },
        }
        fields = UCPResponseParser.extract(body)
        assert fields["latest_fulfillment_event_type"] == "delivered"
        # Raw string preserved verbatim — we only normalize for sort.
        assert fields["latest_fulfillment_event_at"] == "2026-05-09T10:30:00Z"
        # Classifier sees the same instant ordering.
        assert (
            UCPResponseParser.classify(
                "POST",
                "/webhooks/incoming",
                200,
                response_body={"status": "ok"},
                request_body=body,
            )
            == UCPEventType.ORDER_DELIVERED
        )

    def test_adjustment_mixed_z_and_offset_compared_as_instants(self):
        """Same fix applies on the adjustments[] side."""
        body = {
            "id": "order_1",
            "checkout_id": "chk_1",
            "adjustments": [
                {
                    "id": "adj_late",
                    "type": "refund",
                    "occurred_at": "2026-05-09T20:30:00Z",  # 20:30 UTC
                    "status": "completed",
                },
                {
                    "id": "adj_early",
                    "type": "cancellation",
                    "occurred_at": "2026-05-09T22:00:00+02:00",  # 20:00 UTC
                    "status": "completed",
                },
            ],
        }
        fields = UCPResponseParser.extract(body)
        assert fields["latest_adjustment_type"] == "refund"
        assert fields["latest_adjustment_at"] == "2026-05-09T20:30:00Z"

    def test_negative_offset_timestamps(self):
        """Offsets can be negative too. `-05:00` means the local clock
        is 5h behind UTC, so the UTC equivalent is later than the
        wall-clock time suggests."""
        body = {
            "id": "order_1",
            "checkout_id": "chk_1",
            "fulfillment": {
                "events": [
                    # 08:00 in -05:00 zone == 13:00 UTC
                    {
                        "id": "fe_1",
                        "occurred_at": "2026-05-09T08:00:00-05:00",
                        "type": "delivered",
                        "line_items": [],
                    },
                    # 11:00 UTC == 06:00 in -05:00 zone (earlier)
                    {
                        "id": "fe_2",
                        "occurred_at": "2026-05-09T11:00:00Z",
                        "type": "shipped",
                        "line_items": [],
                    },
                ]
            },
        }
        fields = UCPResponseParser.extract(body)
        # 13:00 UTC wins.
        assert fields["latest_fulfillment_event_type"] == "delivered"

    def test_malformed_occurred_at_sorts_behind_valid_timestamps(self):
        """An entry with an unparseable occurred_at must not win over
        an entry with a valid one — even if the malformed string sorts
        higher lexicographically. The valid entry is the only one we
        can trust to express chronology."""
        body = {
            "id": "order_1",
            "checkout_id": "chk_1",
            "fulfillment": {
                "events": [
                    {
                        "id": "fe_valid",
                        "occurred_at": "2026-05-09T17:00:00Z",
                        "type": "delivered",
                        "line_items": [],
                    },
                    {
                        "id": "fe_garbage",
                        "occurred_at": "zzz-not-a-date",
                        "type": "shipped",
                        "line_items": [],
                    },
                ]
            },
        }
        fields = UCPResponseParser.extract(body)
        assert fields["latest_fulfillment_event_type"] == "delivered"

    def test_all_malformed_occurred_at_falls_back_to_last_array_entry(self):
        """If no entry has a parseable occurred_at, the fallback is
        the last array position — append-only contract makes that
        chronologically latest by convention. Still surfacing
        something is better than dropping the column."""
        body = {
            "id": "order_1",
            "checkout_id": "chk_1",
            "fulfillment": {
                "events": [
                    {
                        "id": "fe_1",
                        "type": "shipped",
                        "line_items": [],
                    },
                    {
                        "id": "fe_2",
                        "occurred_at": "not-a-date",
                        "type": "delivered",
                        "line_items": [],
                    },
                ]
            },
        }
        fields = UCPResponseParser.extract(body)
        # Last array entry's type wins on the no-parseable-timestamp
        # fallback.
        assert fields["latest_fulfillment_event_type"] == "delivered"
        # The TIMESTAMP column must NOT carry the raw malformed
        # string — BigQuery would reject the row.
        assert "latest_fulfillment_event_at" not in fields

    def test_naive_timestamp_does_not_crash_classifier(self):
        """`datetime.fromisoformat("2026-05-09T17:00:00")` returns a
        NAIVE datetime (no tz). Comparing naive with aware datetimes
        (from Z / +offset entries in the same array) raises
        ``TypeError: can't compare offset-naive and offset-aware
        datetimes``. _parse_occurred_at must reject naive results so
        the naive entry sorts behind the aware one rather than
        triggering the comparison crash.

        Reviewer's PR #20 second-pass repro: an array mixing a naive
        ISO string with a Z-form string must not crash the
        classifier — the aware entry wins on the comparison."""
        body = {
            "id": "order_1",
            "checkout_id": "chk_1",
            "fulfillment": {
                "events": [
                    # Naive — should sort behind the aware entry.
                    {
                        "id": "fe_1",
                        "occurred_at": "2026-05-09T17:00:00",
                        "type": "shipped",
                        "line_items": [],
                    },
                    # Aware — wins.
                    {
                        "id": "fe_2",
                        "occurred_at": "2026-05-09T12:00:00Z",
                        "type": "delivered",
                        "line_items": [],
                    },
                ]
            },
        }
        # No crash, and the aware entry is the winner.
        fields = UCPResponseParser.extract(body)
        assert fields["latest_fulfillment_event_type"] == "delivered"
        assert fields["latest_fulfillment_event_at"] == "2026-05-09T12:00:00Z"
        # Classifier also doesn't crash and picks the aware entry.
        assert (
            UCPResponseParser.classify(
                "POST",
                "/webhooks/incoming",
                200,
                response_body={"status": "ok"},
                request_body=body,
            )
            == UCPEventType.ORDER_DELIVERED
        )

    def test_all_naive_timestamps_omit_timestamp_column(self):
        """Every entry has a naive (no-timezone) occurred_at. They
        all fail to parse, the fallback picks the last array entry
        for the type column, and the TIMESTAMP column stays absent
        (BigQuery would reject a naive ISO string in a TIMESTAMP
        column too)."""
        body = {
            "id": "order_1",
            "checkout_id": "chk_1",
            "fulfillment": {
                "events": [
                    {
                        "id": "fe_1",
                        "occurred_at": "2026-05-09T08:00:00",
                        "type": "shipped",
                        "line_items": [],
                    },
                    {
                        "id": "fe_2",
                        "occurred_at": "2026-05-09T17:00:00",
                        "type": "delivered",
                        "line_items": [],
                    },
                ]
            },
        }
        fields = UCPResponseParser.extract(body)
        # Type column populated from the fallback (last entry).
        assert fields["latest_fulfillment_event_type"] == "delivered"
        # Timestamp omitted — the naive string is not a valid
        # BigQuery TIMESTAMP value.
        assert "latest_fulfillment_event_at" not in fields

    def test_all_malformed_adjustments_omit_timestamp_column(self):
        """Companion to the fulfillment-side test: malformed
        adjustments[] timestamps must keep type+status populated
        from the fallback entry but skip the TIMESTAMP column."""
        body = {
            "id": "order_1",
            "checkout_id": "chk_1",
            "adjustments": [
                {
                    "id": "adj_1",
                    "type": "refund",
                    "occurred_at": "garbage",
                    "status": "pending",
                },
                {
                    "id": "adj_2",
                    "type": "cancellation",
                    "occurred_at": "also-garbage",
                    "status": "completed",
                },
            ],
        }
        fields = UCPResponseParser.extract(body)
        # Type and status come from the last-array-entry fallback.
        assert fields["latest_adjustment_type"] == "cancellation"
        assert fields["latest_adjustment_status"] == "completed"
        # Timestamp column absent — would reject the row otherwise.
        assert "latest_adjustment_at" not in fields


class TestEmbeddedServicesExtraction:
    """A3 — extract `delegate` and `color_scheme` from embedded
    transport service entries in a `/.well-known/ucp` discovery
    response. Runtime postMessage events (ec.totals.change, reauth,
    etc.) are out of scope; this is the observable-from-server slice
    of the Embedded Checkout protocol surface."""

    def test_single_embedded_service_populates_both_columns(self):
        body = {
            "ucp": {
                "version": "2026-05-09",
                "services": {
                    "dev.ucp.shopping.checkout": [
                        {
                            "transport": "embedded",
                            "schema": "https://merchant.example/schema.json",
                            "config": {
                                "delegate": ["navigate", "submit_form"],
                                "color_scheme": ["light", "dark"],
                            },
                        }
                    ]
                },
            }
        }
        fields = UCPResponseParser.extract(body)
        delegations = json.loads(fields["embedded_delegations_json"])
        assert delegations == ["navigate", "submit_form"]
        schemes = json.loads(fields["embedded_color_schemes_json"])
        assert schemes == ["light", "dark"]

    def test_multiple_embedded_services_union_deduped(self):
        """A platform may offer embedded bindings on multiple
        capabilities (checkout, cart). Each can advertise its own
        `delegate` / `color_scheme` lists; we union across them so
        dashboards see "what does this platform support" without
        joining on capability key. Dedup preserves order of first
        occurrence."""
        body = {
            "ucp": {
                "version": "2026-05-09",
                "services": {
                    "dev.ucp.shopping.checkout": [
                        {
                            "transport": "embedded",
                            "config": {
                                "delegate": ["navigate", "submit_form"],
                                "color_scheme": ["light", "dark"],
                            },
                        }
                    ],
                    "dev.ucp.shopping.cart": [
                        {
                            "transport": "embedded",
                            "config": {
                                # Overlapping `navigate` should dedupe; new
                                # `submit_payment` appends in order.
                                "delegate": ["navigate", "submit_payment"],
                                # Only light — doesn't add anything new.
                                "color_scheme": ["light"],
                            },
                        }
                    ],
                },
            }
        }
        fields = UCPResponseParser.extract(body)
        delegations = json.loads(fields["embedded_delegations_json"])
        assert delegations == ["navigate", "submit_form", "submit_payment"]
        schemes = json.loads(fields["embedded_color_schemes_json"])
        assert schemes == ["light", "dark"]

    def test_non_embedded_transports_ignored(self):
        """Only `transport: embedded` entries contribute. REST / MCP
        / A2A services advertise `endpoint` / `schema` but no embedded
        config, and any spurious `delegate` / `color_scheme` keys on
        them must not leak into the embedded columns."""
        body = {
            "ucp": {
                "version": "2026-05-09",
                "services": {
                    "dev.ucp.shopping.checkout": [
                        {
                            "transport": "rest",
                            "endpoint": "https://merchant.example/api",
                            # Spurious — must NOT be captured.
                            "config": {
                                "delegate": ["should-not-appear"],
                                "color_scheme": ["should-not-appear"],
                            },
                        },
                        {
                            "transport": "mcp",
                            "endpoint": "https://merchant.example/mcp",
                        },
                    ]
                },
            }
        }
        fields = UCPResponseParser.extract(body)
        assert "embedded_delegations_json" not in fields
        assert "embedded_color_schemes_json" not in fields

    def test_embedded_service_with_no_config_omits_columns(self):
        """An embedded service entry without a `config` block
        contributes nothing — both columns stay NULL rather than
        appearing as empty JSON arrays. Preserves the three-state
        signal (NULL = "no data" distinct from `[]` = "explicitly
        empty")."""
        body = {
            "ucp": {
                "version": "2026-05-09",
                "services": {
                    "dev.ucp.shopping.checkout": [
                        {
                            "transport": "embedded",
                            "schema": "https://merchant.example/schema.json",
                        }
                    ]
                },
            }
        }
        fields = UCPResponseParser.extract(body)
        assert "embedded_delegations_json" not in fields
        assert "embedded_color_schemes_json" not in fields

    def test_malformed_config_shapes_skipped_silently(self):
        """A single bad entry mustn't drop the column for the row.
        Non-dict configs, non-list `delegate` / `color_scheme`, and
        non-string list entries are all filtered out; well-formed
        siblings still surface."""
        body = {
            "ucp": {
                "version": "2026-05-09",
                "services": {
                    "dev.ucp.shopping.checkout": [
                        {
                            "transport": "embedded",
                            "config": "not-a-dict",
                        },
                        {
                            "transport": "embedded",
                            "config": {
                                "delegate": "not-a-list",
                                "color_scheme": ["light"],
                            },
                        },
                        {
                            "transport": "embedded",
                            "config": {
                                "delegate": ["navigate", 42, None, "submit_form"],
                                "color_scheme": [None, "dark", 42],
                            },
                        },
                    ]
                },
            }
        }
        fields = UCPResponseParser.extract(body)
        delegations = json.loads(fields["embedded_delegations_json"])
        # Non-string entries silently dropped; well-formed entries kept.
        assert delegations == ["navigate", "submit_form"]
        schemes = json.loads(fields["embedded_color_schemes_json"])
        assert schemes == ["light", "dark"]

    def test_empty_config_arrays_omit_columns(self):
        """`config: {delegate: [], color_scheme: []}` — explicit
        empty lists. We don't ship empty JSON arrays; columns stay
        NULL for a clean three-state signal."""
        body = {
            "ucp": {
                "version": "2026-05-09",
                "services": {
                    "dev.ucp.shopping.checkout": [
                        {
                            "transport": "embedded",
                            "config": {"delegate": [], "color_scheme": []},
                        }
                    ]
                },
            }
        }
        fields = UCPResponseParser.extract(body)
        assert "embedded_delegations_json" not in fields
        assert "embedded_color_schemes_json" not in fields

    def test_no_services_in_ucp_envelope_omits_columns(self):
        body = {"ucp": {"version": "2026-05-09"}}
        fields = UCPResponseParser.extract(body)
        assert "embedded_delegations_json" not in fields
        assert "embedded_color_schemes_json" not in fields

    def test_non_dict_services_value_skipped(self):
        """A capability key pointing at a non-list (malformed sender)
        must not crash the walk."""
        body = {
            "ucp": {
                "version": "2026-05-09",
                "services": {
                    "dev.ucp.shopping.checkout": "not-a-list",
                    "dev.ucp.shopping.cart": [
                        {
                            "transport": "embedded",
                            "config": {"delegate": ["navigate"]},
                        }
                    ],
                },
            }
        }
        fields = UCPResponseParser.extract(body)
        delegations = json.loads(fields["embedded_delegations_json"])
        assert delegations == ["navigate"]


class TestAp2MandateExtraction:
    """A4 — AP2 mandate fields carry cryptographic credentials
    (detached JWS / SD-JWT+kb). Safe-default extraction captures only
    non-PII facts: presence flag, key names, JOSE header fields
    (kid/alg/typ), and SHA-256 of the credential string. The payload
    (credential body) is NEVER decoded or persisted."""

    # A real JOSE header: {"alg":"ES256","kid":"merchant-key-1"}
    # base64url-encoded without padding.
    _MERCHANT_AUTH_HEADER = "eyJhbGciOiJFUzI1NiIsImtpZCI6Im1lcmNoYW50LWtleS0xIn0"
    _MERCHANT_AUTH = f"{_MERCHANT_AUTH_HEADER}..MEUCIQDsignaturedata"
    # SD-JWT+kb: {"alg":"ES256","kid":"buyer-key","typ":"vc+sd-jwt"}
    _CHECKOUT_MANDATE_HEADER = (
        "eyJhbGciOiJFUzI1NiIsImtpZCI6ImJ1eWVyLWtleSIsInR5cCI6InZjK3NkLWp3dCJ9"
    )
    # Body / disclosures / kb-jwt structure (we should NEVER decode any
    # of this beyond the first segment).
    _CHECKOUT_MANDATE = (
        f"{_CHECKOUT_MANDATE_HEADER}"
        ".eyJpZCI6Ik5PVF9ERUNPREVEX1BBWUxPQUQifQ"  # payload (mock)
        ".MEUCIQDsignaturedata"
        "~WyJzYWx0IiwiZmlyc3RfbmFtZSIsIkpvaG4iXQ"  # disclosure (mock)
        "~WyJzYWx0MiIsImxhc3RfbmFtZSIsIkRvZSJd"
    )

    def _extract(self, body):
        """Mimic what the tracker does: call the private helper on
        body.ap2 directly. Parser's public `extract()` deliberately
        skips AP2 to avoid running on a redacted body (per A4's
        decode-from-original-body requirement)."""
        result = {}
        UCPResponseParser._extract_ap2_mandate(body.get("ap2"), result)
        return result

    def test_merchant_authorization_only(self):
        body = {"ap2": {"merchant_authorization": self._MERCHANT_AUTH}}
        fields = self._extract(body)
        assert fields["ap2_mandate_present"] is True
        keys = json.loads(fields["ap2_mandate_keys_json"])
        assert keys == ["merchant_authorization"]
        metadata = json.loads(fields["ap2_mandate_metadata_json"])
        assert "merchant_authorization" in metadata
        ma_meta = metadata["merchant_authorization"]
        # JOSE header decoded — kid + alg, no payload values.
        assert ma_meta["alg"] == "ES256"
        assert ma_meta["kid"] == "merchant-key-1"
        # `typ` absent in this header — only present when sender ships it.
        assert "typ" not in ma_meta
        # SHA-256 of the raw credential string (opaque).
        import hashlib

        expected = hashlib.sha256(self._MERCHANT_AUTH.encode("utf-8")).hexdigest()
        assert ma_meta["sha256"] == expected
        # `checkout_mandate` not present.
        assert "checkout_mandate" not in metadata

    def test_both_mandates_present(self):
        body = {
            "ap2": {
                "merchant_authorization": self._MERCHANT_AUTH,
                "checkout_mandate": self._CHECKOUT_MANDATE,
            }
        }
        fields = self._extract(body)
        assert fields["ap2_mandate_present"] is True
        keys = json.loads(fields["ap2_mandate_keys_json"])
        # Order-preserving: matches _AP2_MANDATE_FIELDS order.
        assert keys == ["merchant_authorization", "checkout_mandate"]
        metadata = json.loads(fields["ap2_mandate_metadata_json"])
        assert metadata["merchant_authorization"]["alg"] == "ES256"
        assert metadata["checkout_mandate"]["alg"] == "ES256"
        assert metadata["checkout_mandate"]["kid"] == "buyer-key"
        # The SD-JWT typ is preserved (helps disambiguate credential
        # types in dashboards).
        assert metadata["checkout_mandate"]["typ"] == "vc+sd-jwt"

    def test_no_ap2_field_columns_absent(self):
        body = {"id": "chk_123", "status": "ready_for_complete"}
        fields = self._extract(body)
        assert "ap2_mandate_present" not in fields
        assert "ap2_mandate_keys_json" not in fields
        assert "ap2_mandate_metadata_json" not in fields

    def test_empty_ap2_dict_columns_absent(self):
        # body.ap2 exists but has no mandate fields.
        body = {"ap2": {}}
        fields = self._extract(body)
        assert "ap2_mandate_present" not in fields
        assert "ap2_mandate_keys_json" not in fields

    def test_metadata_extraction_does_not_decode_payload(self):
        """Critical safety: only the FIRST segment (JOSE header) is
        decoded. The payload / disclosures / kb-jwt are never decoded.
        Pin that no field from the mock payload string
        `NOT_DECODED_PAYLOAD` appears anywhere in the extracted
        fields — proves we never base64-decoded the second segment."""
        body = {
            "ap2": {
                "checkout_mandate": self._CHECKOUT_MANDATE,
            }
        }
        fields = self._extract(body)
        # Serialize all extracted fields to one string and assert
        # the canary value from the payload never appears anywhere.
        serialized = json.dumps(fields)
        assert "NOT_DECODED_PAYLOAD" not in serialized
        assert "first_name" not in serialized
        assert "last_name" not in serialized
        # And John / Doe (from the mock disclosures).
        assert "John" not in serialized
        assert "Doe" not in serialized

    def test_malformed_credential_string_no_dot_skipped(self):
        """A bad sender might ship a non-JWS string for the credential.
        We skip the metadata decode (no header to decode) but the
        presence/keys/sha256 still populate — analytics records what
        was shipped, doesn't crash."""
        body = {"ap2": {"merchant_authorization": "not-a-jws"}}
        fields = self._extract(body)
        assert fields["ap2_mandate_present"] is True
        assert json.loads(fields["ap2_mandate_keys_json"]) == ["merchant_authorization"]
        metadata = json.loads(fields["ap2_mandate_metadata_json"])
        # No JOSE header → no kid/alg/typ, but sha256 still present.
        import hashlib

        expected = hashlib.sha256(b"not-a-jws").hexdigest()
        assert metadata["merchant_authorization"] == {"sha256": expected}

    def test_malformed_credential_base64_failure_skipped(self):
        """Credential has a `.` but the first segment isn't valid
        base64url. JOSE decode silently fails; sha256 still works."""
        body = {"ap2": {"merchant_authorization": "!!!.payload.signature"}}
        fields = self._extract(body)
        metadata = json.loads(fields["ap2_mandate_metadata_json"])
        # No header fields (base64 failure), but the credential is
        # still hashed.
        assert "sha256" in metadata["merchant_authorization"]
        assert "alg" not in metadata["merchant_authorization"]
        assert "kid" not in metadata["merchant_authorization"]

    def test_non_dict_ap2_skipped(self):
        body = {"ap2": "not-a-dict"}
        fields = self._extract(body)
        assert "ap2_mandate_present" not in fields

    def test_non_string_credential_value_skipped(self):
        body = {"ap2": {"merchant_authorization": 42}}
        fields = self._extract(body)
        # Field IS in the dict (so it's present), but metadata is
        # empty (no decode possible, no sha possible).
        assert fields["ap2_mandate_present"] is True
        assert "ap2_mandate_metadata_json" not in fields

    def test_unrecognized_ap2_field_ignored(self):
        """Only `merchant_authorization` and `checkout_mandate` are
        recognized. A future / custom field on the AP2 namespace
        doesn't trip the presence flag — the keys list explicitly
        names which mandates the sender shipped."""
        body = {
            "ap2": {
                "custom_field": "some-value",
            }
        }
        fields = self._extract(body)
        # No recognized mandate → no columns populate.
        assert "ap2_mandate_present" not in fields


class TestBuyerConsentExtraction:
    """A4 — buyer.consent is the privacy-preference subobject. We
    capture ONLY it, never the parent buyer object which carries
    PII (first_name, last_name, email, phone_number)."""

    def _extract(self, body):
        result = {}
        UCPResponseParser._extract_buyer_consent(body.get("buyer"), result)
        return result

    def test_consent_extracted(self):
        body = {
            "buyer": {
                "first_name": "John",
                "last_name": "Doe",
                "email": "john@example.com",
                "phone_number": "+15551234567",
                "consent": {
                    "analytics": True,
                    "preferences": True,
                    "marketing": False,
                    "sale_of_data": False,
                },
            }
        }
        fields = self._extract(body)
        # Consent subobject captured.
        consent = json.loads(fields["buyer_consent_json"])
        assert consent == {
            "analytics": True,
            "preferences": True,
            "marketing": False,
            "sale_of_data": False,
        }
        # Critical: NO buyer PII anywhere in the extracted fields.
        serialized = json.dumps(fields)
        assert "John" not in serialized
        assert "Doe" not in serialized
        assert "john@example.com" not in serialized
        assert "+15551234567" not in serialized
        # And the parent buyer object is NOT surfaced as its own column.
        assert "buyer_json" not in fields

    def test_no_consent_subobject_column_absent(self):
        body = {
            "buyer": {
                "first_name": "John",
                "email": "john@example.com",
            }
        }
        fields = self._extract(body)
        # buyer present but no consent → column NULL.
        assert "buyer_consent_json" not in fields
        # And no PII leaks regardless.
        serialized = json.dumps(fields)
        assert "John" not in serialized
        assert "john@example.com" not in serialized

    def test_no_buyer_at_all_column_absent(self):
        body = {"id": "chk_xyz"}
        fields = self._extract(body)
        assert "buyer_consent_json" not in fields

    def test_empty_consent_omits_column(self):
        body = {"buyer": {"consent": {}}}
        fields = self._extract(body)
        # Explicit empty consent — column stays NULL (no signal to
        # record). Distinct from "consent not provided".
        assert "buyer_consent_json" not in fields

    def test_non_dict_consent_skipped(self):
        body = {"buyer": {"consent": "not-a-dict"}}
        fields = self._extract(body)
        assert "buyer_consent_json" not in fields

    def test_non_dict_buyer_skipped(self):
        body = {"buyer": "not-a-dict"}
        fields = self._extract(body)
        assert "buyer_consent_json" not in fields

    def test_unknown_keys_inside_consent_dropped(self):
        """Reviewer's PR-23 repro: a malformed or extended sender
        might nest PII fields (`email`, `phone_number`, etc.) inside
        the consent subobject. Without a whitelist, those would land
        in the safe-by-default JSON column before _redact() runs.

        Only the four documented consent flags survive the filter
        (analytics / preferences / marketing / sale_of_data);
        everything else is silently dropped."""
        body = {
            "buyer": {
                "consent": {
                    "analytics": True,
                    "preferences": False,
                    "email": "nested@example.com",
                    "phone_number": "+15551234567",
                    "first_name": "Smuggled",
                    "metadata": {"tracking_id": "leaked"},
                    "ip_addresses": ["10.0.0.1"],
                }
            }
        }
        fields = self._extract(body)
        consent = json.loads(fields["buyer_consent_json"])
        # Only whitelisted flags survive.
        assert consent == {"analytics": True, "preferences": False}
        # And no PII leaks through the serialization.
        serialized = json.dumps(fields)
        assert "nested@example.com" not in serialized
        assert "+15551234567" not in serialized
        assert "Smuggled" not in serialized
        assert "leaked" not in serialized
        assert "10.0.0.1" not in serialized

    def test_non_boolean_consent_values_dropped(self):
        """Consent flags are spec'd as booleans. A sender that ships
        a string / number / dict for one of the whitelisted keys
        gets that entry dropped — analytics records only well-formed
        boolean flags. Otherwise a malformed sender could smuggle
        strings via a documented key name."""
        body = {
            "buyer": {
                "consent": {
                    "analytics": "yes",  # string, not bool
                    "preferences": 1,  # int, not bool
                    "marketing": False,  # valid bool — should survive
                    "sale_of_data": {"value": True},  # dict, not bool
                }
            }
        }
        fields = self._extract(body)
        consent = json.loads(fields["buyer_consent_json"])
        # Only the well-formed boolean survives.
        assert consent == {"marketing": False}

    def test_all_keys_unknown_omits_column(self):
        """If every key in consent fails the whitelist (rare, but
        possible if a sender renames everything or only ships PII),
        the column stays NULL rather than serializing an empty dict.
        Three-state signal: NULL = "no recognized consent data",
        distinct from `{}` = "explicitly empty"."""
        body = {
            "buyer": {
                "consent": {
                    "email": "nested@example.com",
                    "custom_flag": True,  # bool but unknown key
                }
            }
        }
        fields = self._extract(body)
        assert "buyer_consent_json" not in fields
        # PII didn't leak.
        assert "nested@example.com" not in json.dumps(fields)


class TestSignalsSafeDefaultExtraction:
    """A6 — Authorization & abuse signals. The signals dict is open
    (`additionalProperties: true`) with reverse-domain key naming.
    Safe-default extraction captures presence + key names only;
    values are NEVER persisted in these columns. Raw capture is
    gated on the tracker's `include_signals_raw` flag and lives in
    test_tracker.py."""

    def test_present_and_keys_extracted(self):
        body = {
            "signals": {
                "dev.ucp.buyer_ip": "192.0.2.1",
                "dev.ucp.user_agent": "Mozilla/5.0 (privacy-sensitive)",
            }
        }
        fields = UCPResponseParser.extract(body)
        assert fields["signals_present"] is True
        keys = json.loads(fields["signals_keys_json"])
        assert keys == ["dev.ucp.buyer_ip", "dev.ucp.user_agent"]
        # CRITICAL: values must NEVER appear in safe-default fields.
        serialized = json.dumps(fields)
        assert "192.0.2.1" not in serialized
        assert "Mozilla/5.0" not in serialized
        assert "privacy-sensitive" not in serialized

    def test_operator_extended_signal_keys_recorded(self):
        """`additionalProperties: true` — operators can ship custom
        signals using reverse-domain keys. The key names are
        observability (which signals were sent); the values stay
        out of safe defaults."""
        body = {
            "signals": {
                "dev.ucp.buyer_ip": "10.0.0.1",
                "dev.merchant.session_token": "secret-token",
                "dev.merchant.device_fingerprint": "fp-abc-123",
            }
        }
        fields = UCPResponseParser.extract(body)
        keys = json.loads(fields["signals_keys_json"])
        # All three keys present in order.
        assert keys == [
            "dev.ucp.buyer_ip",
            "dev.merchant.session_token",
            "dev.merchant.device_fingerprint",
        ]
        # No values leak.
        serialized = json.dumps(fields)
        assert "10.0.0.1" not in serialized
        assert "secret-token" not in serialized
        assert "fp-abc-123" not in serialized

    def test_no_signals_field_columns_absent(self):
        body = {"id": "chk_123"}
        fields = UCPResponseParser.extract(body)
        assert "signals_present" not in fields
        assert "signals_keys_json" not in fields

    def test_empty_signals_dict_columns_absent(self):
        """Three-state semantics: explicit empty dict → NULL,
        not `{present: True, keys: []}`. Distinct from "signals
        observed but empty"."""
        body = {"signals": {}}
        fields = UCPResponseParser.extract(body)
        assert "signals_present" not in fields
        assert "signals_keys_json" not in fields

    def test_non_dict_signals_skipped(self):
        body = {"signals": "not-a-dict"}
        fields = UCPResponseParser.extract(body)
        assert "signals_present" not in fields

    def test_non_string_keys_dropped_from_keys_list(self):
        """A malformed sender that ships integer/None keys (Python
        would accept these in a dict, but the spec is reverse-domain
        strings) gets those entries dropped from the keys list.
        Presence flag still True so we don't lose visibility of the
        delivery."""
        body = {
            "signals": {
                "dev.ucp.buyer_ip": "192.0.2.1",
                42: "non-string-key",
            }
        }
        fields = UCPResponseParser.extract(body)
        assert fields["signals_present"] is True
        keys = json.loads(fields["signals_keys_json"])
        # Only the well-formed key survives.
        assert keys == ["dev.ucp.buyer_ip"]

    def test_signal_with_complex_value_does_not_leak(self):
        """A signal value might be a dict / list / nested structure
        (e.g. a fingerprint blob). Safe-default columns only carry
        the key name — the value is never serialized."""
        body = {
            "signals": {
                "dev.merchant.fingerprint": {
                    "canvas_hash": "abc123",
                    "webgl_renderer": "RTX-secret",
                    "ip_addresses": ["10.0.0.1"],
                }
            }
        }
        fields = UCPResponseParser.extract(body)
        keys = json.loads(fields["signals_keys_json"])
        assert keys == ["dev.merchant.fingerprint"]
        serialized = json.dumps(fields)
        assert "abc123" not in serialized
        assert "RTX-secret" not in serialized
        assert "10.0.0.1" not in serialized


class TestCheckoutStatusScoping:
    """Tests that checkout_status is only set for checkout responses."""

    def test_checkout_status_set_for_checkout(self):
        body = {"id": "chk_123", "status": "completed"}
        fields = UCPResponseParser.extract(body)
        assert fields["checkout_status"] == "completed"

    def test_checkout_status_not_set_for_order(self):
        body = {
            "id": "order_xyz",
            "checkout_id": "chk_abc",
            "status": "shipped",
        }
        fields = UCPResponseParser.extract(body)
        assert "checkout_status" not in fields

    def test_checkout_status_not_set_for_unknown_status(self):
        body = {"id": "cart_abc", "status": "active"}
        fields = UCPResponseParser.extract(body)
        assert "checkout_status" not in fields

    def test_checkout_status_requires_escalation(self):
        body = {"id": "chk_123", "status": "requires_escalation"}
        fields = UCPResponseParser.extract(body)
        assert fields["checkout_status"] == "requires_escalation"
