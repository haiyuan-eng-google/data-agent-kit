"""UCP event types and data model.

Types are aligned with UCP spec capabilities (checkout, order, identity linking)
and the checkout state machine (incomplete → requires_escalation →
ready_for_complete → completed | canceled).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class UCPEventType(str, Enum):
    """UCP event types aligned with protocol capabilities and operations."""

    # Checkout lifecycle (maps to REST operations on /checkout-sessions)
    CHECKOUT_SESSION_CREATED = "checkout_session_created"
    CHECKOUT_SESSION_GET = "checkout_session_get"
    CHECKOUT_SESSION_UPDATED = "checkout_session_updated"
    CHECKOUT_SESSION_COMPLETED = "checkout_session_completed"
    CHECKOUT_SESSION_CANCELED = "checkout_session_canceled"
    CHECKOUT_ESCALATION = "checkout_escalation"

    # Cart lifecycle (maps to REST operations on /carts)
    CART_CREATED = "cart_created"
    CART_GET = "cart_get"
    CART_UPDATED = "cart_updated"
    CART_CANCELED = "cart_canceled"

    # Catalog discovery (maps to REST operations on /catalog/*)
    CATALOG_SEARCH = "catalog_search"
    CATALOG_LOOKUP = "catalog_lookup"
    CATALOG_PRODUCT_GET = "catalog_product_get"

    # Order lifecycle (webhook-based in UCP)
    ORDER_CREATED = "order_created"
    ORDER_UPDATED = "order_updated"
    # B5: REST GET /orders/{id} reads order state without mutating it.
    # Distinct from ORDER_UPDATED (REST-driven PUT) and ORDER_*
    # lifecycle types (state transitions). Without this, a plain
    # poll on the order endpoint was misclassified as an update,
    # inflating the "% of rows that mutated the order" KPI.
    ORDER_GET = "order_get"
    ORDER_SHIPPED = "order_shipped"
    ORDER_DELIVERED = "order_delivered"
    ORDER_RETURNED = "order_returned"
    ORDER_CANCELED = "order_canceled"
    # Generic order-webhook receipt: emitted when a webhook delivery is
    # detected (by configured path prefix or by Standard Webhooks
    # headers) but the body carries no recognizable lifecycle status.
    # Distinct from ORDER_UPDATED, which is for REST-driven order
    # updates — pivoting taxonomy on URL format conflated those two.
    ORDER_WEBHOOK_RECEIVED = "order_webhook_received"

    # Identity linking (OAuth 2.0)
    IDENTITY_LINK_INITIATED = "identity_link_initiated"
    IDENTITY_LINK_COMPLETED = "identity_link_completed"
    IDENTITY_LINK_REVOKED = "identity_link_revoked"

    # Payment
    PAYMENT_HANDLER_NEGOTIATED = "payment_handler_negotiated"
    PAYMENT_INSTRUMENT_SELECTED = "payment_instrument_selected"
    PAYMENT_COMPLETED = "payment_completed"
    PAYMENT_FAILED = "payment_failed"

    # Discovery & capability negotiation
    PROFILE_DISCOVERED = "profile_discovered"
    CAPABILITY_NEGOTIATED = "capability_negotiated"

    # Generic HTTP-level events (fallback)
    REQUEST = "request"
    ERROR = "error"


class CheckoutStatus(str, Enum):
    """UCP checkout session statuses (spec-defined state machine)."""

    INCOMPLETE = "incomplete"
    REQUIRES_ESCALATION = "requires_escalation"
    READY_FOR_COMPLETE = "ready_for_complete"
    COMPLETE_IN_PROGRESS = "complete_in_progress"
    COMPLETED = "completed"
    CANCELED = "canceled"


# ---------------------------------------------------------------------------
# Event data class
# ---------------------------------------------------------------------------


@dataclass
class UCPEvent:
    """A single UCP analytics event row destined for BigQuery.

    Fields are a superset covering all UCP capabilities.  Unused fields
    remain None and are dropped before BigQuery insert.
    """

    # --- identity ---
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # --- context ---
    app_name: str = ""
    merchant_host: str = ""  # business endpoint host
    # platform_profile_url stores the raw UCP-Agent header value
    # (e.g. `profile="https://platform.example/profile"`). The name is
    # misleading on business → platform webhook flows where the header
    # carries the business's profile — kept for backwards compatibility.
    # New queries should prefer ucp_agent_profile_url, which carries the
    # parsed URI only.
    platform_profile_url: str = ""
    transport: str = "rest"  # rest | mcp | a2a | embedded

    # --- A3: Embedded Checkout (server-observable slice) ---
    # The transport enum has listed `embedded` for a while but we
    # captured nothing embedded-specific. Two server-observable
    # signals from this library: (a) discovery responses where a
    # `transport: embedded` service entry carries an
    # EmbeddedTransportConfig with `delegate` and `color_scheme`
    # lists; (b) the `ec_color_scheme` URL query parameter the host
    # sends when fetching the embedded checkout page.
    #
    # Runtime postMessage events (link delegation acceptance,
    # `ec.totals.change`, error alignment, reauth, cart binding)
    # are NOT capturable from server-side HTTP — they live in the
    # iframe / host browser and need separate host instrumentation
    # to land in analytics. Out of scope for this slice.
    embedded_delegations_json: Optional[str] = None
    embedded_color_schemes_json: Optional[str] = None
    embedded_ec_color_scheme: Optional[str] = None

    # --- A4: AP2 mandates + Buyer Consent ---
    # AP2 (Agent Payments Protocol) extends Checkout with cryptographic
    # mandates: `merchant_authorization` is a detached JWS (RFC 7515
    # App F) and `checkout_mandate` is an SD-JWT+KB credential. Both
    # carry signed claims about the buyer / merchant; capturing them
    # verbatim would land sensitive credentials in analytics.
    #
    # Safe-by-default columns observe only non-PII facts:
    #   * `ap2_mandate_present` -- whether ANY mandate field is present
    #   * `ap2_mandate_keys_json` -- which mandate fields are present
    #     (`["merchant_authorization", "checkout_mandate"]`)
    #   * `ap2_mandate_metadata_json` -- JOSE-header facts only
    #     (`kid`, `alg`, `typ`) plus SHA-256 hex of the credential
    #     string. NEVER the payload (the credential body), NEVER the
    #     disclosures.
    #   * `buyer_consent_json` -- ONLY `body.buyer.consent` (the
    #     shape-only consent flags), NEVER the full buyer object
    #     which carries PII (first_name, last_name, email,
    #     phone_number).
    #
    # Opt-in raw column (only when tracker `include_ap2_raw=True`):
    #   * `ap2_mandate_raw_json` -- original `body.ap2` object passed
    #     through `_redact`, which has `merchant_authorization` and
    #     `checkout_mandate` in the default pii_fields so credentials
    #     are redacted even with the opt-in. Disabled by default.
    ap2_mandate_present: Optional[bool] = None
    ap2_mandate_keys_json: Optional[str] = None
    ap2_mandate_metadata_json: Optional[str] = None
    buyer_consent_json: Optional[str] = None
    ap2_mandate_raw_json: Optional[str] = None

    # --- A6: Authorization & abuse signals ---
    # `signals.json` at c5c6139 declares a `body.signals` dict keyed
    # by reverse-domain names with `additionalProperties: true`.
    # Known PII signals: `dev.ucp.buyer_ip` (IP address) and
    # `dev.ucp.user_agent` (user-agent string). Operators can ship
    # additional reverse-domain keys for merchant-specific signals.
    #
    # Safe-by-default columns capture only non-PII facts:
    #   * `signals_present` -- whether any signals are present
    #   * `signals_keys_json` -- the NAMES of signal keys (never
    #     the values, which may carry IP / user-agent / fingerprint
    #     data)
    #
    # Opt-in raw column (only when tracker `include_signals_raw=True`):
    #   * `signals_json` -- original `body.signals` passed through
    #     `_redact`. The known PII signal field names are
    #     force-included in pii_fields so their values are scrubbed
    #     regardless of `redact_pii` setting or operator-provided
    #     pii_fields lists. Operators can extend the redaction set
    #     with additional reverse-domain keys via the existing
    #     pii_fields constructor parameter.
    signals_present: Optional[bool] = None
    signals_keys_json: Optional[str] = None
    signals_json: Optional[str] = None

    # --- HTTP ---
    http_method: str = ""
    http_path: str = ""
    http_status_code: Optional[int] = None
    idempotency_key: str = ""
    request_id: str = ""

    # --- checkout ---
    checkout_session_id: Optional[str] = None
    checkout_status: Optional[str] = None
    order_id: Optional[str] = None

    # --- context (UCP request-body Context object) ---
    # source/schemas/shopping/types/context.json — capture intent/language/
    # currency as scalars and eligibility as a JSON blob. Address fields
    # (address_country, address_region, postal_code) are PII and deferred
    # to a later slice with the redaction policy.
    context_intent: Optional[str] = None
    context_language: Optional[str] = None
    context_currency: Optional[str] = None
    context_eligibility_json: Optional[str] = None

    # --- HTTP message signing (RFC 9421 / UCP signatures.md) ---
    # request_signed / response_signed flag whether a Signature-Input
    # header was present on the corresponding side; the keyid columns
    # carry the first parsable `keyid` parameter so dashboards can
    # join against the JWK published at `/.well-known/ucp`'s
    # `signing_keys[]`. Algorithm columns (C5c) are derived from the
    # matched JWK's `crv` field via an operator-provided jwk_lookup
    # callable on the tracker — UCP signatures.md states the algorithm
    # is NOT in `Signature-Input` and must be looked up from the JWK.
    # NULL when no jwk_lookup is configured, no keyid was extracted,
    # the lookup misses, or the curve doesn't map to a known JWA alg.
    request_signed: Optional[bool] = None
    response_signed: Optional[bool] = None
    request_signature_keyid: Optional[str] = None
    response_signature_keyid: Optional[str] = None
    request_signature_alg: Optional[str] = None
    response_signature_alg: Optional[str] = None

    # --- Standard Webhooks metadata (UCP order.md) ---
    # Captured from `Webhook-Id` and `Webhook-Timestamp` request headers
    # on inbound webhook deliveries. webhook_timestamp is parsed from
    # Unix seconds into an ISO 8601 UTC string for BQ TIMESTAMP storage
    # — the spec specifies Unix seconds, not ISO 8601.
    webhook_id: Optional[str] = None
    webhook_timestamp: Optional[str] = None

    # --- UCP-Agent profile (RFC 8941 Dictionary, parsed) ---
    # The `profile` URI extracted from the UCP-Agent header. Replaces
    # the misnamed `platform_profile_url` (which stored the raw header
    # string) for new queries — `platform_profile_url` is kept above
    # for backwards compatibility but the name is wrong on
    # business → platform webhook flows.
    ucp_agent_profile_url: Optional[str] = None

    # --- WWW-Authenticate Bearer challenge (RFC 7235 / RFC 6750) ---
    # Captured from the response side on auth-failure rows. realm is
    # the protected-resource identifier; error is the challenge code
    # (e.g. invalid_token, insufficient_scope); scope is the required
    # OAuth 2.0 scope string; resource_metadata is the RFC 9728
    # pointer to OAuth protected-resource metadata. The set of fields
    # actually populated depends on what the issuer sent — the helper
    # only writes a column when the param is present.
    auth_challenge_error: Optional[str] = None
    auth_challenge_scope: Optional[str] = None
    auth_challenge_realm: Optional[str] = None
    auth_challenge_resource_metadata: Optional[str] = None

    # --- financial (minor units / cents, spec total types) ---
    currency: Optional[str] = None
    items_discount_amount: Optional[int] = None
    subtotal_amount: Optional[int] = None
    discount_amount: Optional[int] = None
    fulfillment_amount: Optional[int] = None
    tax_amount: Optional[int] = None
    fee_amount: Optional[int] = None
    total_amount: Optional[int] = None
    # B1 / C9: full verbatim totals array. Scalar amount columns
    # above are SUM(amount) per well-known type; this column
    # preserves the original ordered entries (with `display_text`,
    # `lines[]` itemization, duplicates, business-defined types)
    # so dashboards can compute multi-tax / refund-line / business-
    # type analytics without losing the per-line trail.
    totals_json: Optional[str] = None

    # --- line items ---
    line_items_json: Optional[str] = None
    line_item_count: Optional[int] = None

    # --- payment ---
    payment_handler_id: Optional[str] = None
    payment_instrument_type: Optional[str] = None
    payment_brand: Optional[str] = None
    # Per-handler available_instruments registry sourced from
    # body.ucp.payment_handlers[*].available_instruments (the
    # handler-declaration site per payment_handler.json), not from
    # body.payment.* (which is selected/submitted instruments). All
    # handlers preserved with per-handler instrument arrays intact —
    # downstream queries can pivot on handler id or instrument type.
    payment_available_instruments_json: Optional[str] = None

    # --- capabilities & extensions ---
    ucp_version: Optional[str] = None
    capabilities_json: Optional[str] = None
    extensions_json: Optional[str] = None

    # --- identity linking ---
    identity_provider: Optional[str] = None
    identity_scope: Optional[str] = None

    # --- fulfillment ---
    fulfillment_type: Optional[str] = None
    fulfillment_destination_country: Optional[str] = None

    # --- discount extension ---
    discount_codes_json: Optional[str] = None
    discount_applied_json: Optional[str] = None

    # --- checkout metadata ---
    expires_at: Optional[str] = None
    continue_url: Optional[str] = None

    # --- order ---
    permalink_url: Optional[str] = None
    # C7: optional human-readable label per `order.json` (PR #326
    # upstream). Business-set only — agents / platforms must not
    # populate. Surfaced only on order-shaped bodies (carry
    # `checkout_id`) so a stray `label` on a checkout body doesn't
    # get misattributed.
    order_label: Optional[str] = None

    # --- order lifecycle (B8: fulfillment.events[] + adjustments[]) ---
    # At UCP order.md/c5c6139 the order has no top-level `status`;
    # lifecycle lives in two append-only arrays. Full arrays
    # preserved as JSON for multi-event KPIs (e.g., time from shipped
    # to delivered, refund rate over time); narrow "latest" scalars
    # surface the current state for fast-pivot dashboards.
    fulfillment_events_json: Optional[str] = None
    adjustments_json: Optional[str] = None
    latest_fulfillment_event_type: Optional[str] = None
    latest_fulfillment_event_at: Optional[str] = None
    latest_adjustment_type: Optional[str] = None
    latest_adjustment_status: Optional[str] = None
    latest_adjustment_at: Optional[str] = None

    # --- messages / errors ---
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_severity: Optional[str] = None
    messages_json: Optional[str] = None
    # Deduped lists of message codes by severity. Stored as JSON arrays
    # so dashboards can pivot via JSON_QUERY_ARRAY without regex over
    # the full messages_json blob. identity_optional_present is a
    # convenience flag (PR #354 info code) — answers "% of unauthed
    # sessions where login would have unlocked more capabilities" with
    # a single-column predicate.
    message_info_codes_json: Optional[str] = None
    message_warning_codes_json: Optional[str] = None
    identity_optional_present: Optional[bool] = None

    # A5 — Eligibility verification outcome (PR #250). The three codes
    # come from messages[].code; UCP doesn't prescribe verification, so
    # the outcome is observed not asserted. Three-state nullable BOOLs:
    # TRUE when this code is the observed outcome, FALSE when a
    # different eligibility outcome fired (mutually exclusive trio),
    # NULL when no eligibility outcome code surfaced. Dashboards can
    # compose any "verified" notion from the trio + messages_json.
    eligibility_accepted_present: Optional[bool] = None
    eligibility_not_accepted_present: Optional[bool] = None
    eligibility_invalid_present: Optional[bool] = None

    # --- performance ---
    latency_ms: Optional[float] = None

    # --- custom ---
    custom_metadata_json: Optional[str] = None

    def to_bq_row(self) -> Dict[str, Any]:
        """Serialize to a BigQuery-insertable dict (drop None fields)."""
        return {k: v for k, v in self.__dict__.items() if v is not None}
