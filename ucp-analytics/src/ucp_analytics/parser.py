"""Parse UCP JSON responses into structured analytics fields.

Understands the checkout object schema, totals array, payment instruments,
fulfillment extension, discount extension, messages array, and the
ucp metadata envelope.

Aligned with the official UCP specification at
https://github.com/Universal-Commerce-Protocol/ucp
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ucp_analytics._headers import credential_sha256, decode_jose_header
from ucp_analytics._path_match import is_webhook_delivery
from ucp_analytics.events import UCPEventType

# A4: AP2 mandate field names. Both are cryptographic credentials
# (detached JWS / SD-JWT+kb) and must never appear verbatim in
# analytics — captured only via SHA-256 hash + JOSE header in the
# safe-default columns, and only via the redacted raw column if the
# operator opts in.
_AP2_MANDATE_FIELDS = ("merchant_authorization", "checkout_mandate")
# Non-secret JOSE header fields we surface from each mandate. `kid`
# identifies the signing key, `alg` the algorithm, `typ` the
# credential type (e.g. `vc+sd-jwt`). Anything else in the header
# is platform-specific and not part of the analytics contract.
_AP2_SAFE_HEADER_FIELDS = ("kid", "alg", "typ")

# A4: known buyer.consent flags (the only fields we serialize from
# the consent subobject). A strict whitelist — a malformed or
# extended sender that nests PII fields like `email` inside consent
# would otherwise leak them into the safe-by-default column before
# any redaction runs. We also restrict values to booleans (the
# consent spec is shape-only flags). Anything off-shape is silently
# dropped, preserving signal fidelity for the known flags.
_BUYER_CONSENT_FIELDS = frozenset(
    {"analytics", "preferences", "marketing", "sale_of_data"}
)

# A6: known PII signal keys per UCP `signals.json` at c5c6139.
# The signals dict is open (`additionalProperties: true`) with
# reverse-domain key naming, so operators can ship additional
# signals; these are the documented ones that MUST be redacted in
# any raw capture. The tracker force-includes these in pii_fields.
_KNOWN_PII_SIGNAL_KEYS = frozenset({"dev.ucp.buyer_ip", "dev.ucp.user_agent"})

# A5 — Eligibility verification outcome codes. Per UCP `eligibility.md`,
# verification outcomes surface through `messages[].code` rather than a
# structured boolean. The trio is mutually exclusive in well-formed
# responses but the codes span severities: `eligibility_invalid` is
# canonically `error` (it appears in upstream `error_code` examples),
# while `eligibility_accepted` / `eligibility_not_accepted` are
# typically `info`. Walking only one severity would miss the others, so
# the eligibility capture is cross-severity.
_ELIGIBILITY_OUTCOME_CODES = frozenset(
    {
        "eligibility_accepted",
        "eligibility_not_accepted",
        "eligibility_invalid",
    }
)

# B8 — Order lifecycle derived from fulfillment.events[] and
# adjustments[]. At c5c6139 `order.json` no longer carries a top-level
# `status`; the append-only `fulfillment.events[]` log is the
# authoritative shipment state and `adjustments[]` captures
# post-order events (refunds / returns / disputes / cancellations).
# Per the FulfillmentEvent / Adjustment schemas, `type` is an open
# string with documented common values; we map the common values that
# correspond to our existing ORDER_* event types and leave anything
# else to fall through to ORDER_WEBHOOK_RECEIVED.
_FULFILLMENT_EVENT_TYPE_TO_EVENT = {
    "shipped": UCPEventType.ORDER_SHIPPED,
    "in_transit": UCPEventType.ORDER_SHIPPED,
    "delivered": UCPEventType.ORDER_DELIVERED,
    "returned_to_sender": UCPEventType.ORDER_RETURNED,
    "canceled": UCPEventType.ORDER_CANCELED,
    "cancelled": UCPEventType.ORDER_CANCELED,
    "undeliverable": UCPEventType.ORDER_CANCELED,
}
_ADJUSTMENT_TYPE_TO_EVENT = {
    "return": UCPEventType.ORDER_RETURNED,
    "refund": UCPEventType.ORDER_RETURNED,
    "cancellation": UCPEventType.ORDER_CANCELED,
}


def _parse_occurred_at(value: Any) -> Optional[datetime]:
    """Parse an RFC 3339 ``occurred_at`` string into an aware datetime.

    Accepts both the ``Z`` (UTC) and ``±HH:MM`` offset forms that
    RFC 3339 permits. Returns None on missing / malformed input so
    callers can sort unparseable entries behind valid ones rather
    than crashing on them.

    ``datetime.fromisoformat`` only learned to accept ``Z`` directly
    in Python 3.11; we normalize ``...Z`` → ``...+00:00`` first so
    the helper works on older runtimes too.

    Naive datetimes (no tzinfo) are treated as invalid. RFC 3339
    requires a timezone designator on every timestamp; without one
    we'd compare a naive datetime against the aware datetimes from
    Z/offset entries and Python raises ``TypeError`` ("can't compare
    offset-naive and offset-aware datetimes"). Treating naive values
    as invalid sorts them behind well-formed entries via the
    ``_latest_by_occurred_at`` fallback path, preserving extraction
    rather than crashing.
    """
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def _latest_by_occurred_at(items: Any) -> Optional[dict]:
    """Return the entry with the latest ``occurred_at`` from a UCP event
    array, or None if the array is empty / malformed.

    Append-only event logs (FulfillmentEvent, Adjustment) should be in
    chronological order on the wire, but a sender that doesn't preserve
    order (or that injects late-arriving deliveries) shouldn't break
    analytics. Lexicographic sort on the raw RFC 3339 string would be
    wrong when entries use mixed offset forms — e.g.
    ``2026-05-09T10:30:00Z`` and ``2026-05-09T12:00:00+02:00``
    represent the same UTC instant 10:00 / 10:30, but the string sort
    flips them. Parse to aware datetimes for comparison so the
    comparison reflects the actual instant. The returned dict keeps
    its original ``occurred_at`` string verbatim — we only normalize
    for the ordering decision, not for downstream consumers.

    Tiebreak / fallback:
      * Entries whose ``occurred_at`` is missing or unparseable sort
        behind all valid entries.
      * Among entries with equal parsed timestamps (rare but possible
        in batched deliveries), later array position wins — preserving
        the append-only ordering.
      * If no entry has a parseable ``occurred_at``, fall back to the
        last array position so we still surface *something* rather
        than dropping the column entirely.
    """
    if not isinstance(items, list) or not items:
        return None
    valid = [x for x in items if isinstance(x, dict)]
    if not valid:
        return None
    parsed = [(item, _parse_occurred_at(item.get("occurred_at"))) for item in valid]
    dated = [(item, ts) for item, ts in parsed if ts is not None]
    if dated:
        # Iterate so that later array positions win on equal timestamps
        # (`>=` instead of `>`). Append-only contract means later
        # position in the array is later in chronological order when
        # the wire timestamp ties.
        latest_item, latest_ts = dated[0]
        for item, ts in dated[1:]:
            if ts >= latest_ts:
                latest_item, latest_ts = item, ts
        return latest_item
    # No parseable timestamps — fall back to the last array entry.
    return valid[-1]


def _lifecycle_event_from_order_body(body: dict) -> Optional[UCPEventType]:
    """Derive an ORDER_* lifecycle event type from a new-shape order body.

    Priority:
      1. `fulfillment.events[]` latest entry's `type` — authoritative
         shipment state (delivered overrides shipped overrides in_transit).
      2. `adjustments[]` latest entry's `type` — post-order events
         (return, refund, cancellation) when no fulfillment event
         carries lifecycle info.
      3. Legacy top-level `status` — pre-c5c6139 payloads / senders
         that still ship the flat shape.

    Returns None when none of the above identify a known lifecycle
    transition; the caller then falls back to ORDER_WEBHOOK_RECEIVED
    (B5b) or ORDER_UPDATED (REST PUT).
    """
    fulfillment = body.get("fulfillment")
    if isinstance(fulfillment, dict):
        latest = _latest_by_occurred_at(fulfillment.get("events"))
        if latest:
            event = _FULFILLMENT_EVENT_TYPE_TO_EVENT.get(
                str(latest.get("type") or "").lower()
            )
            if event:
                return event

    latest_adj = _latest_by_occurred_at(body.get("adjustments"))
    if latest_adj:
        event = _ADJUSTMENT_TYPE_TO_EVENT.get(str(latest_adj.get("type") or "").lower())
        if event:
            return event

    # Legacy top-level status (pre-c5c6139). Kept as a fallback so
    # senders that still ship the flat shape continue to classify
    # correctly; new-shape senders override via the branches above.
    legacy_status = body.get("status", "")
    if legacy_status == "shipped":
        return UCPEventType.ORDER_SHIPPED
    if legacy_status == "delivered":
        return UCPEventType.ORDER_DELIVERED
    if legacy_status == "returned":
        return UCPEventType.ORDER_RETURNED
    if legacy_status in ("canceled", "cancelled"):
        return UCPEventType.ORDER_CANCELED

    return None


class UCPResponseParser:
    """Extract analytics-relevant fields from UCP request/response bodies."""

    # ------------------------------------------------------------------ #
    # Classify event type from HTTP method + path + body
    # ------------------------------------------------------------------ #

    @classmethod
    def classify(
        cls,
        method: str,
        path: str,
        status_code: int,
        response_body: Optional[dict],
        request_body: Optional[dict] = None,
        request_headers: Optional[Mapping[str, str]] = None,
        webhook_path_prefixes: Iterable[str] = (),
    ) -> UCPEventType:
        """Derive the UCP event type from the HTTP request + response.

        ``request_headers`` and ``webhook_path_prefixes`` widen webhook
        detection beyond the default `/webhook(s)` prefix so platforms
        that advertise `order.config.webhook_url` as `/events`,
        `/ucp-events`, `/hooks/<id>`, etc. (per UCP `order.md`,
        ``"The URL format is platform-specific"``) still classify as
        order-webhook deliveries. Optional with backwards-compatible
        defaults so direct callers and existing tests don't have to
        thread them.
        """
        m = method.upper()
        p = path.rstrip("/")

        # /.well-known/ucp  →  discovery
        if p.endswith("/.well-known/ucp"):
            return UCPEventType.PROFILE_DISCOVERED

        # OAuth / OpenID metadata discovery (RFC 8414, OIDC, RFC 9728).
        # These are the start of an identity-linking flow — the platform
        # fetches the business's auth metadata before driving an OAuth dance.
        if (
            p.endswith("/.well-known/oauth-authorization-server")
            or p.endswith("/.well-known/openid-configuration")
            or p.endswith("/.well-known/oauth-protected-resource")
        ):
            return UCPEventType.IDENTITY_LINK_INITIATED

        # /checkout-sessions  POST  → created
        if re.search(r"/checkout-sessions/?$", p) and m == "POST":
            return UCPEventType.CHECKOUT_SESSION_CREATED

        # /checkout-sessions/{id}/complete  POST  → completed
        if re.search(r"/checkout-sessions/[^/]+/complete$", p) and m == "POST":
            return UCPEventType.CHECKOUT_SESSION_COMPLETED

        # /checkout-sessions/{id}/cancel  POST  → canceled
        if re.search(r"/checkout-sessions/[^/]+/cancel$", p) and m == "POST":
            return UCPEventType.CHECKOUT_SESSION_CANCELED

        # /checkout-sessions/{id}  PUT  → updated (or escalation)
        if re.search(r"/checkout-sessions/[^/]+$", p) and m == "PUT":
            if response_body and response_body.get("status") == "requires_escalation":
                return UCPEventType.CHECKOUT_ESCALATION
            return UCPEventType.CHECKOUT_SESSION_UPDATED

        # /checkout-sessions/{id}  GET  → get
        if re.search(r"/checkout-sessions/[^/]+$", p) and m == "GET":
            return UCPEventType.CHECKOUT_SESSION_GET

        # /carts  POST  → created
        if re.search(r"/carts/?$", p) and m == "POST":
            return UCPEventType.CART_CREATED

        # /carts/{id}/cancel  POST  → canceled
        if re.search(r"/carts/[^/]+/cancel$", p) and m == "POST":
            return UCPEventType.CART_CANCELED

        # /carts/{id}  PUT  → updated
        if re.search(r"/carts/[^/]+$", p) and m == "PUT":
            return UCPEventType.CART_UPDATED

        # /carts/{id}  GET  → get
        if re.search(r"/carts/[^/]+$", p) and m == "GET":
            return UCPEventType.CART_GET

        # /catalog/{search,lookup,product}  POST  → catalog discovery
        if m == "POST":
            if re.search(r"/catalog/search/?$", p):
                return UCPEventType.CATALOG_SEARCH
            if re.search(r"/catalog/lookup/?$", p):
                return UCPEventType.CATALOG_LOOKUP
            if re.search(r"/catalog/product/?$", p):
                return UCPEventType.CATALOG_PRODUCT_GET

        # Order webhook detection. UCP `order.md` says the URL format
        # is platform-specific, so we can't rely on a fixed `/webhooks`
        # prefix. Two signals enter this branch:
        #   1. Path matches `/webhook(s)` or any operator-configured
        #      `webhook_path_prefixes` — for path-aware deployments.
        #   2. Standard Webhooks `Webhook-Id` + `Webhook-Timestamp`
        #      headers are both present — for header-aware fallback
        #      regardless of URL. UCP `order.md` requires both on
        #      every order-event webhook.
        # Either alone is sufficient; together they're the same branch.
        #
        # Webhook detection runs BEFORE the `/orders` REST branch so a
        # platform that advertises `/webhooks/orders` as its webhook
        # URL classifies as a webhook (the more-specific signal) rather
        # than as a REST `/orders` endpoint (which the trailing
        # `/orders$` regex would otherwise match). `is_webhook_delivery`
        # suppresses on known UCP REST paths, so plain `/orders/{id}`
        # without webhook headers still falls through to the REST
        # branch below.
        if is_webhook_delivery(p, request_headers, webhook_path_prefixes):
            # Webhook errors still classify as errors.
            if status_code and status_code >= 400:
                return UCPEventType.ERROR
            # Lifecycle event types are derived from the *payload*, not
            # the URL — the issue #8 B5b directive: "we don't pivot
            # taxonomy on URL format". Webhooks ship the order body in
            # the request, with a small ack in the response.
            body = (
                request_body
                if request_body and isinstance(request_body, dict)
                else response_body
            )
            if body and isinstance(body, dict):
                lifecycle = _lifecycle_event_from_order_body(body)
                if lifecycle:
                    return lifecycle
            # Legacy URL-segment fallback for senders that don't
            # include status (or fulfillment.events[]) in the body.
            # Kept for back-compat with platforms that still publish
            # `/webhooks/order-delivered`-style URLs; the body-driven
            # path above takes precedence so a new-shape sender that
            # includes fulfillment.events overrides the URL heuristic.
            if re.search(r"/order[_-]delivered", p):
                return UCPEventType.ORDER_DELIVERED
            if re.search(r"/order[_-]returned", p):
                return UCPEventType.ORDER_RETURNED
            if re.search(r"/order[_-]canceled", p):
                return UCPEventType.ORDER_CANCELED
            # Generic webhook receipt — body has no recognizable
            # lifecycle status and URL has no segment hint.
            # Distinct from ORDER_UPDATED (which is for REST-driven
            # PUT /orders/{id}); analytics needs to tell those apart
            # because they have different traffic shapes (webhooks
            # are platform→business with signing; REST updates are
            # business→platform).
            return UCPEventType.ORDER_WEBHOOK_RECEIVED

        # /orders (strict: /orders or /orders/{id}, not /reorder etc.)
        if re.search(r"/orders(?:/[^/]+)?$", p):
            if m == "POST":
                return UCPEventType.ORDER_CREATED
            # Lifecycle derivation: prefer the new-shape
            # `fulfillment.events[]` / `adjustments[]` arrays (c5c6139)
            # and fall back to legacy top-level `status`. The helper
            # encapsulates all three branches. Lifecycle wins on
            # both GET and PUT — a GET that returns a delivered
            # order still classifies as ORDER_DELIVERED.
            if response_body and isinstance(response_body, dict):
                lifecycle = _lifecycle_event_from_order_body(response_body)
                if lifecycle:
                    return lifecycle
            # B5: GET /orders/{id} is a read-only poll, distinct from
            # ORDER_UPDATED (which is REST-driven PUT mutation).
            # Without this branch, plain reads inflate the
            # "% of order rows that mutated the order" KPI.
            if m == "GET":
                return UCPEventType.ORDER_GET
            return UCPEventType.ORDER_UPDATED

        # Identity linking (strict: /identity, /oauth, or /oauth2 paths).
        # The trailing oauth2? in the regex is necessary because /oauth
        # alone wouldn't match /oauth2/token — the segment boundary
        # `(?:/|$)` fails after `oauth` when the next char is `2`.
        if re.search(r"/(?:identity|oauth2?)(?:/|$)", p):
            # /identity/revoke, /oauth2/revoke, or DELETE → revoked
            if "/revoke" in p or m == "DELETE":
                return UCPEventType.IDENTITY_LINK_REVOKED
            # OAuth callback or OAuth token endpoint finalize the link
            if "/callback" in p or "/oauth2/token" in p:
                return UCPEventType.IDENTITY_LINK_COMPLETED
            return UCPEventType.IDENTITY_LINK_INITIATED

        # Simulate shipping (samples server testing endpoint)
        if "/simulate-shipping" in p:
            return UCPEventType.ORDER_SHIPPED

        # Errors
        if status_code and status_code >= 400:
            return UCPEventType.ERROR

        return UCPEventType.REQUEST

    # ------------------------------------------------------------------ #
    # JSON-RPC classification (MCP / A2A transports)
    # ------------------------------------------------------------------ #

    # Map tool/action names to equivalent HTTP method + path
    _TOOL_TO_HTTP: Dict[str, tuple] = {
        # MCP tool names
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
        "discover": ("GET", "/.well-known/ucp"),
        "discover_merchant": ("GET", "/.well-known/ucp"),
        "simulate_shipping": ("POST", "/testing/simulate-shipping/{id}"),
        "order_event_webhook": ("POST", "/webhooks/partners/{id}/events/order"),
        "add_to_checkout": ("PUT", "/checkout-sessions/{id}"),
        "remove_from_checkout": ("PUT", "/checkout-sessions/{id}"),
        "update_customer_details": ("PUT", "/checkout-sessions/{id}"),
        "start_payment": ("PUT", "/checkout-sessions/{id}"),
        "link_identity": ("POST", "/identity"),
        "revoke_identity": ("DELETE", "/identity/revoke"),
        "negotiate_capability": ("POST", "/capabilities/negotiate"),
        # A2A action prefixes (a2a.ucp.*)
        "a2a.ucp.checkout.create": ("POST", "/checkout-sessions"),
        "a2a.ucp.checkout.update": ("PUT", "/checkout-sessions/{id}"),
        "a2a.ucp.checkout.complete": ("POST", "/checkout-sessions/{id}/complete"),
        "a2a.ucp.checkout.cancel": ("POST", "/checkout-sessions/{id}/cancel"),
        "a2a.ucp.checkout.get": ("GET", "/checkout-sessions/{id}"),
        "a2a.ucp.cart.create": ("POST", "/carts"),
        "a2a.ucp.cart.update": ("PUT", "/carts/{id}"),
        "a2a.ucp.cart.cancel": ("POST", "/carts/{id}/cancel"),
        "a2a.ucp.cart.get": ("GET", "/carts/{id}"),
        "a2a.ucp.catalog.search": ("POST", "/catalog/search"),
        "a2a.ucp.catalog.lookup": ("POST", "/catalog/lookup"),
        "a2a.ucp.catalog.product": ("POST", "/catalog/product"),
        "a2a.ucp.order.create": ("POST", "/orders"),
        "a2a.ucp.order.get": ("GET", "/orders/{id}"),
        "a2a.ucp.order.update": ("PUT", "/orders/{id}"),
        "a2a.ucp.discover": ("GET", "/.well-known/ucp"),
        "a2a.ucp.identity.link": ("POST", "/identity"),
        "a2a.ucp.identity.revoke": ("DELETE", "/identity/revoke"),
        "a2a.ucp.capability.negotiate": ("POST", "/capabilities/negotiate"),
    }

    @classmethod
    def classify_jsonrpc(
        cls,
        tool_name: str,
        status_code: int = 200,
        response_body: Optional[dict] = None,
    ) -> UCPEventType:
        """Classify a JSON-RPC tool/action name into a UCP event type.

        Used for MCP (tools/call) and A2A (tasks/send) transports.
        Maps tool names to HTTP equivalents, then delegates to classify().
        """
        # Capability negotiation keywords (check before _TOOL_TO_HTTP
        # since /capabilities/negotiate doesn't match classify() patterns)
        if "negotiate" in tool_name or "capability" in tool_name:
            return UCPEventType.CAPABILITY_NEGOTIATED

        mapping = cls._TOOL_TO_HTTP.get(tool_name)
        if mapping:
            method, path = mapping
            return cls.classify(method, path, status_code, response_body)

        # Handle A2A DataPart keys like "add_to_checkout" → update
        if "add_to" in tool_name or "remove_from" in tool_name or "update" in tool_name:
            if "checkout" in tool_name:
                return cls.classify(
                    "PUT", "/checkout-sessions/{id}", status_code, response_body
                )
            if "cart" in tool_name:
                return cls.classify("PUT", "/carts/{id}", status_code, response_body)

        return UCPEventType.REQUEST

    # ------------------------------------------------------------------ #
    # Extract checkout & commerce fields from a UCP JSON body
    # ------------------------------------------------------------------ #

    @classmethod
    def extract(cls, body: Optional[dict]) -> Dict[str, Any]:
        """Extract analytics fields from a UCP checkout/order JSON body.

        Works with both request bodies (partial) and response bodies (full).
        Returns a dict of field_name → value; callers merge into UCPEvent.
        """
        if not body or not isinstance(body, dict):
            return {}

        result: Dict[str, Any] = {}

        # --- session / order id ---
        raw_id = body.get("id", "")
        id_str = str(raw_id) if raw_id else ""
        if id_str:
            # Heuristic: order objects have checkout_id; checkout objects don't
            if "checkout_id" in body:
                result["order_id"] = id_str
                result["checkout_session_id"] = body["checkout_id"]
                # C7: optional `label` per `order.json` (PR #326). Only
                # surface it when the body is order-shaped (carries
                # checkout_id) so a stray `label` on a checkout body
                # doesn't get misattributed. Business-set only per spec.
                label = body.get("label")
                if isinstance(label, str) and label:
                    result["order_label"] = label
            else:
                result["checkout_session_id"] = id_str

        if "order_id" in body:
            result["order_id"] = body["order_id"]

        # --- order confirmation in checkout response (spec: checkout.order) ---
        order_obj = body.get("order")
        if isinstance(order_obj, dict):
            order_id = order_obj.get("id")
            if order_id:
                result["order_id"] = str(order_id)
            permalink = order_obj.get("permalink_url")
            if permalink:
                result["permalink_url"] = permalink

        # --- permalink_url (direct on order objects) ---
        if "permalink_url" in body:
            result["permalink_url"] = body["permalink_url"]

        # --- status ---
        if "status" in body:
            # Only write checkout_status for checkout responses, not orders/carts
            if "checkout_id" not in body:
                status_val = body["status"]
                _CHECKOUT_STATUSES = {
                    "incomplete",
                    "requires_escalation",
                    "ready_for_complete",
                    "complete_in_progress",
                    "completed",
                    "canceled",
                }
                if status_val in _CHECKOUT_STATUSES:
                    result["checkout_status"] = status_val

        # --- currency ---
        if "currency" in body:
            result["currency"] = body["currency"]

        # --- totals array ---
        cls._extract_totals(body.get("totals"), result)

        # --- line items ---
        items = body.get("line_items")
        if isinstance(items, list) and items:
            result["line_item_count"] = len(items)
            result["line_items_json"] = json.dumps(items, default=str)

        # --- ucp metadata envelope ---
        cls._extract_ucp_metadata(body.get("ucp"), result)

        # --- A3: embedded transport config from ucp.services[*] ---
        cls._extract_embedded_services(body.get("ucp"), result)

        # --- A6: authorization / abuse signals safe-default ---
        # Only presence + key names. The values may carry IP /
        # user-agent / fingerprint data and never appear in the
        # safe-default columns. Raw signals capture (gated on
        # tracker `include_signals_raw`) lives in the tracker.
        cls._extract_signals(body.get("signals"), result)

        # NOTE: A4 (AP2 mandate metadata + buyer consent) is NOT
        # extracted here. Those fields need the un-redacted body to
        # compute SHA-256 / decode JOSE headers, while this `extract`
        # method runs AFTER the tracker's general PII redaction.
        # The tracker calls `_extract_ap2_mandate` and
        # `_extract_buyer_consent` directly on the original body.

        # --- payment_handlers registry from ucp metadata ---
        cls._extract_payment_available_instruments(body.get("ucp"), result)

        # --- context (UCP request-body Context object) ---
        cls._extract_context_fields(body.get("context"), result)

        # --- discovery: payment.handlers at top level (sibling of ucp) ---
        cls._extract_discovery_payment(body.get("payment"), result)

        # --- payment (spec: payment.instruments[], fallback: payment.handlers[]) ---
        cls._extract_payment(body, result)

        # --- fulfillment extension ---
        cls._extract_fulfillment(body.get("fulfillment"), result)

        # --- order lifecycle (B8): fulfillment.events[] + adjustments[] ---
        # At c5c6139 the order has no top-level `status`; lifecycle
        # lives in two append-only arrays. We preserve both verbatim
        # as JSON columns and derive narrow "latest" scalars for
        # fast-pivot dashboards.
        cls._extract_order_lifecycle(body, result)

        # --- discount extension ---
        cls._extract_discounts(body.get("discounts"), result)

        # --- checkout metadata ---
        if "expires_at" in body:
            result["expires_at"] = body["expires_at"]
        if "continue_url" in body:
            result["continue_url"] = body["continue_url"]

        # --- identity linking ---
        if "provider" in body:
            result["identity_provider"] = body["provider"]
        if "scope" in body:
            result["identity_scope"] = body["scope"]
        # Nested identity object
        identity = body.get("identity")
        if isinstance(identity, dict):
            if "provider" in identity:
                result["identity_provider"] = identity["provider"]
            if "scope" in identity:
                result["identity_scope"] = identity["scope"]

        # --- messages (errors / warnings / info from the server) ---
        # Single pass over messages[]. Captures the first error for
        # the legacy error_* columns plus per-severity deduped code
        # lists for dashboards that pivot on info/warning codes
        # (e.g. "% of sessions emitting identity_optional"). Order-
        # preserving dedup so the JSON arrays match the on-the-wire
        # order, modulo duplicates.
        messages = body.get("messages")
        if isinstance(messages, list) and messages:
            result["messages_json"] = json.dumps(messages, default=str)
            first_error_seen = False
            info_codes: List[str] = []
            warning_codes: List[str] = []
            seen_info: set = set()
            seen_warning: set = set()
            seen_eligibility: set = set()
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_type = msg.get("type")
                code = msg.get("code")
                if msg_type == "error" and not first_error_seen:
                    result["error_code"] = code
                    result["error_message"] = msg.get("content")
                    result["error_severity"] = msg.get("severity")
                    first_error_seen = True
                elif msg_type == "info" and isinstance(code, str) and code:
                    if code not in seen_info:
                        seen_info.add(code)
                        info_codes.append(code)
                elif msg_type == "warning" and isinstance(code, str) and code:
                    if code not in seen_warning:
                        seen_warning.add(code)
                        warning_codes.append(code)

                # A5 — eligibility outcome codes are cross-severity:
                # `eligibility_invalid` is canonically `error`, the
                # other two are typically `info`. Capture from any
                # severity (separate `if`, not `elif`) so the error
                # branch above doesn't suppress them.
                if isinstance(code, str) and code in _ELIGIBILITY_OUTCOME_CODES:
                    seen_eligibility.add(code)
            if info_codes:
                result["message_info_codes_json"] = json.dumps(info_codes)
                # Convenience flag for the C11 KPI ("% of unauthed
                # sessions where auth would unlock more capabilities").
                # Three-state nullable BOOL semantics:
                #   True  — info codes observed AND identity_optional
                #           is among them
                #   False — info codes observed AND identity_optional
                #           is NOT among them
                #   NULL  — no info codes observed at all (no row-level
                #           denominator contribution)
                # Setting the flag to bool() of the membership check
                # keeps the KPI denominator honest:
                #   COUNT(identity_optional_present) is "rows that
                #   could have signaled identity_optional", and
                #   COUNT(identity_optional_present = TRUE) is the
                #   numerator.
                result["identity_optional_present"] = "identity_optional" in seen_info
            if warning_codes:
                result["message_warning_codes_json"] = json.dumps(warning_codes)

            # A5 — three-state eligibility outcome flags.
            # Per UCP eligibility.md the verification outcome is
            # signalled by one of three message codes; the trio is
            # mutually exclusive in well-formed responses. Three-state
            # nullable BOOL semantics mirror identity_optional_present
            # (C11), with the denominator being "we observed at least
            # one eligibility outcome code in this row":
            #   True  — this code is among the seen eligibility codes
            #   False — at least one eligibility outcome code was
            #           observed but it was NOT this one (the trio is
            #           mutually exclusive, so when one fires the
            #           other two are concretely absent)
            #   NULL  — no eligibility outcome code observed at all;
            #           verification may not have run, or the code
            #           may not have surfaced through messages — no
            #           row-level denominator contribution
            # Setting the trio together keeps the dashboard math
            # honest: COUNT(eligibility_*_present) is "rows where
            # eligibility verification surfaced an outcome", and each
            # column's TRUE count is its outcome's numerator.
            if seen_eligibility:
                result["eligibility_accepted_present"] = (
                    "eligibility_accepted" in seen_eligibility
                )
                result["eligibility_not_accepted_present"] = (
                    "eligibility_not_accepted" in seen_eligibility
                )
                result["eligibility_invalid_present"] = (
                    "eligibility_invalid" in seen_eligibility
                )

        # --- links ---
        links = body.get("links")
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict) and link.get("type") == "order":
                    result["order_id"] = result.get("order_id") or link.get("url")

        # Drop None values
        return {k: v for k, v in result.items() if v is not None}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    # B1 / C9 mapping. `total.json` is an open vocabulary with these
    # well-known type values; businesses MAY use additional types
    # (captured verbatim via `totals_json`). Each scalar column is
    # SUM(amount) over all entries of the matching type — `total.json`
    # permits multiple detail rows per type plus `lines[]`
    # itemization, so last-wins assignment would silently drop
    # split-tax / multi-line-discount data.
    _TOTALS_TYPE_TO_COLUMN = {
        "items_discount": "items_discount_amount",
        "subtotal": "subtotal_amount",
        "discount": "discount_amount",
        "fulfillment": "fulfillment_amount",
        "tax": "tax_amount",
        "fee": "fee_amount",
        "total": "total_amount",
    }

    @classmethod
    def _extract_totals(cls, totals: Any, result: Dict[str, Any]) -> None:
        """Parse the UCP totals array into individual amount fields.

        Spec total types (well-known, open vocabulary): items_discount,
        subtotal, discount, fulfillment, tax, fee, total. Businesses
        MAY use additional values per `total.json`; the verbatim array
        rides on `totals_json` for downstream analysis.

        Amounts integer-only and signed per `signed_amount.json`
        (positive for charges, negative for refunds). Multiple
        detail rows of the same type SUM into the scalar column —
        e.g. split state+local tax rows accumulate into
        `tax_amount`. Without SUM, a sender that ships two tax
        entries would last-wins-drop one of them.
        """
        if not isinstance(totals, list):
            return
        # Preserve full ordered array (every entry, including
        # duplicates / `display_text` / `lines[]` / business-defined
        # types) for dashboards that need per-line trails or refund
        # breakdowns. Filter to dict entries so JSON_QUERY downstream
        # doesn't choke.
        clean = [item for item in totals if isinstance(item, dict)]
        if clean:
            result["totals_json"] = json.dumps(clean, default=str)
        # Per-type SUM aggregation into scalar columns.
        for item in totals:
            if not isinstance(item, dict):
                continue
            t_type = item.get("type", "")
            amount = item.get("amount")
            if not isinstance(amount, int) or isinstance(amount, bool):
                # Reject non-int amounts (incl. bool, which is an
                # int subclass — `True + 100` would silently
                # corrupt). String/float amounts are out-of-spec
                # and dropped; well-formed siblings still survive.
                continue
            column = cls._TOTALS_TYPE_TO_COLUMN.get(t_type)
            if column is None:
                continue
            result[column] = result.get(column, 0) + amount

    @classmethod
    def _extract_context_fields(cls, context: Any, result: Dict[str, Any]) -> None:
        """Extract analytics fields from a UCP request-body Context object.

        Spec ref: ``source/schemas/shopping/types/context.json`` — top-level
        properties are ``{address_country, address_region, postal_code,
        intent, language, currency, eligibility}``. We capture intent /
        language / currency as scalars and eligibility as a JSON blob of
        reverse-domain identifiers. The three address fields are PII and
        deferred to a later slice that lands the redaction policy alongside.
        """
        if not isinstance(context, dict):
            return
        intent = context.get("intent")
        if intent:
            result["context_intent"] = intent
        language = context.get("language")
        if language:
            result["context_language"] = language
        currency = context.get("currency")
        if currency:
            result["context_currency"] = currency
        eligibility = context.get("eligibility")
        if isinstance(eligibility, list) and eligibility:
            result["context_eligibility_json"] = json.dumps(eligibility, default=str)

    @classmethod
    def _extract_payment_available_instruments(
        cls, ucp_meta: Any, result: Dict[str, Any]
    ) -> None:
        """Capture body.ucp.payment_handlers[*].available_instruments.

        Spec source is `payment_handler.json` (the *handler declaration*
        site) — distinct from `body.payment.*` which carries the selected
        instrument on a checkout/order. Stored in full so downstream
        queries can pivot on handler id or instrument type.

        Handles both the array and dict-keyed registry shapes via
        `_normalize_registry`, the same as capabilities. Handlers
        without an `available_instruments` array are dropped from the
        stored payload — keeping them would inflate the column with
        empty entries that aren't useful for analytics.
        """
        if not isinstance(ucp_meta, dict):
            return
        handlers_raw = ucp_meta.get("payment_handlers")
        if not handlers_raw:
            return
        handlers = cls._normalize_registry(handlers_raw)
        # Preserve all handlers that actually declare an instrument
        # registry; collapse the rest so the column reflects the
        # "instruments offered" surface, not the handler list.
        # Schema-tight: `available_instruments` is an array per
        # payment_handler.json. A malformed truthy value (string,
        # dict, etc.) would otherwise survive the truthy check and
        # break dashboard JSON_QUERY_ARRAY assumptions on this column.
        with_instruments = [
            h
            for h in handlers
            if isinstance(h, dict)
            and isinstance(h.get("available_instruments"), list)
            and h["available_instruments"]
        ]
        if with_instruments:
            result["payment_available_instruments_json"] = json.dumps(
                with_instruments, default=str
            )

    @classmethod
    def _extract_ucp_metadata(cls, ucp_meta: Any, result: Dict[str, Any]) -> None:
        """Parse the UCP metadata envelope.

        Per the Python SDK and samples, capabilities are arrays of objects
        with a ``name`` field::

            [{"name": "dev.ucp.shopping.checkout", "version": "2026-01-11"}]

        For robustness, also handles an object-keyed format where capability
        names are dict keys (e.g. ``{"dev.ucp.shopping.checkout": [...]}``)
        by normalizing to the array format.
        """
        if not isinstance(ucp_meta, dict):
            return

        result["ucp_version"] = ucp_meta.get("version")

        caps_raw = ucp_meta.get("capabilities")
        if caps_raw:
            caps_list = cls._normalize_registry(caps_raw)
            if caps_list:
                result["capabilities_json"] = json.dumps(caps_list, default=str)

    @classmethod
    def _extract_embedded_services(cls, ucp_meta: Any, result: Dict[str, Any]) -> None:
        """Aggregate embedded-transport config across `ucp.services[*]`.

        Discovery responses (`/.well-known/ucp`) carry a service
        registry keyed by capability reverse-domain name; each value
        is a list of service bindings, one per transport. Embedded
        bindings carry an `EmbeddedTransportConfig` block under
        `config` with two fields we surface for dashboards:

          * `delegate`     — link delegations the business allows
                             (e.g. ``["navigate", "submit_form"]``)
          * `color_scheme` — themes the business supports
                             (subset of ``["light", "dark"]``)

        We union both fields across all embedded services in the
        response, order-preserving and deduped — answering questions
        like "% of platforms supporting dark mode" or "what
        delegations are most commonly granted" with a single column.
        Per-service detail stays available via the existing
        `capabilities_json` column if dashboards need to disaggregate.

        Non-embedded transport entries (rest / mcp / a2a) are ignored.
        Malformed config shapes (non-dict config, non-list delegate /
        color_scheme, non-string entries) are skipped silently so a
        single buggy service doesn't drop the column for the row.
        """
        if not isinstance(ucp_meta, dict):
            return
        services = ucp_meta.get("services")
        if not isinstance(services, dict):
            return

        delegations: List[str] = []
        color_schemes: List[str] = []
        seen_delegations: set = set()
        seen_color_schemes: set = set()

        for entries in services.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("transport") != "embedded":
                    continue
                config = entry.get("config")
                if not isinstance(config, dict):
                    continue
                # `isinstance(... , list)` is load-bearing: strings
                # are iterable in Python, so a sender that ships
                # `delegate: "navigate"` (string, not list) would
                # otherwise iterate characters and pass the
                # isinstance(str) check on each one. We accept only
                # explicit lists.
                raw_delegate = config.get("delegate")
                if isinstance(raw_delegate, list):
                    for delegate in raw_delegate:
                        if (
                            isinstance(delegate, str)
                            and delegate not in seen_delegations
                        ):
                            seen_delegations.add(delegate)
                            delegations.append(delegate)
                raw_color_scheme = config.get("color_scheme")
                if isinstance(raw_color_scheme, list):
                    for scheme in raw_color_scheme:
                        if isinstance(scheme, str) and scheme not in seen_color_schemes:
                            seen_color_schemes.add(scheme)
                            color_schemes.append(scheme)

        if delegations:
            result["embedded_delegations_json"] = json.dumps(delegations)
        if color_schemes:
            result["embedded_color_schemes_json"] = json.dumps(color_schemes)

    @classmethod
    def _extract_signals(cls, signals: Any, result: Dict[str, Any]) -> None:
        """Capture safe-default metadata from ``body.signals``.

        Per ``signals.json`` at c5c6139, the signals object is a
        reverse-domain-keyed dict with ``additionalProperties: true``.
        Known PII signals (``dev.ucp.buyer_ip``,
        ``dev.ucp.user_agent``) live alongside operator-extended
        signals using the same shape.

        Safe-default extraction surfaces only:
          * presence (BOOL) -- whether any signals are present
          * key names (JSON array of strings) -- the signal IDs that
            were observed. Never the values, which carry IP /
            user-agent / fingerprint data.

        Insertion order is preserved (dict iteration order is the
        wire order on Python 3.7+) so dashboards reading the JSON
        array see the signals in the order the sender shipped them.

        Defensive: non-dict ``signals`` is skipped silently; an
        empty dict leaves the columns NULL rather than serializing
        an empty array (three-state: NULL = "no signals observed",
        distinct from "signals shipped but empty").

        Non-string keys (malformed senders) are dropped from the
        keys list; the BOOL `signals_present` still reflects the
        original dict's emptiness so we don't drop the row entirely
        on a bad sender.
        """
        if not isinstance(signals, dict) or not signals:
            return
        # Presence is true as long as any entry was shipped; the
        # operator may want to know about malformed deliveries.
        result["signals_present"] = True
        keys = [k for k in signals.keys() if isinstance(k, str)]
        if keys:
            result["signals_keys_json"] = json.dumps(keys)

    @classmethod
    def _extract_ap2_mandate(cls, ap2: Any, result: Dict[str, Any]) -> None:
        """Capture safe-default metadata from ``body.ap2`` mandates.

        AP2 (Agent Payments Protocol) extends Checkout with two
        cryptographic mandate fields under ``body.ap2``:

          * ``merchant_authorization`` -- a detached JWS (RFC 7515
            App F) over the checkout body, format ``<header>..<sig>``
            (note the double-dot: empty payload, since the payload is
            the body itself).
          * ``checkout_mandate`` -- an SD-JWT+kb credential, format
            ``<header>.<payload>.<sig>~<disclosure>~...~<kb-jwt>``.

        Both carry signed claims about the buyer or merchant.
        Capturing them verbatim would land sensitive credentials in
        analytics. This extractor observes only:

          * presence (BOOL)
          * which keys are present (JSON array of mandate field
            names — already public, just observability of which
            mandates the platform shipped)
          * JOSE-header facts (``kid``, ``alg``, ``typ``) -- decoded
            from the FIRST segment of each credential, which is the
            base64url-encoded JOSE header. We never decode the
            payload (second segment) or the disclosures.
          * SHA-256 hex of the full credential string -- treats the
            credential as opaque so dashboards can correlate the
            same credential across rows without persisting it.

        Defensive: malformed shapes (non-dict ``ap2``, non-string
        credential values, base64url decode failures, non-JSON
        header bytes) are skipped silently so a single bad sender
        doesn't drop the row.
        """
        if not isinstance(ap2, dict):
            return

        present_keys: List[str] = [
            field for field in _AP2_MANDATE_FIELDS if field in ap2
        ]
        if not present_keys:
            return

        result["ap2_mandate_present"] = True
        result["ap2_mandate_keys_json"] = json.dumps(present_keys)

        metadata: Dict[str, Dict[str, Any]] = {}
        for field in present_keys:
            credential = ap2.get(field)
            entry: Dict[str, Any] = {}
            header = decode_jose_header(credential)
            if header:
                for header_field in _AP2_SAFE_HEADER_FIELDS:
                    value = header.get(header_field)
                    if isinstance(value, str) and value:
                        entry[header_field] = value
            sha = credential_sha256(credential)
            if sha:
                entry["sha256"] = sha
            if entry:
                metadata[field] = entry
        if metadata:
            result["ap2_mandate_metadata_json"] = json.dumps(metadata)

    @classmethod
    def _extract_buyer_consent(cls, buyer: Any, result: Dict[str, Any]) -> None:
        """Capture ONLY ``body.buyer.consent`` -- not the parent buyer.

        Per ``buyer.json``, the buyer object carries PII (first_name,
        last_name, email, phone_number). The consent subobject is
        shape-only flags (analytics / preferences / marketing /
        sale_of_data). Capturing the consent subobject gives
        dashboards the privacy-preference signal without persisting
        PII.

        Strict whitelist on both keys and value types:
          * Only the four documented consent flags are serialized
            (analytics / preferences / marketing / sale_of_data).
            A malformed or extended sender that nests PII fields
            (e.g. ``consent.email``) would otherwise leak PII into
            the safe-by-default column before any redaction runs.
          * Values are restricted to ``bool``. The consent spec is
            shape-only flags; non-boolean values (strings, dicts,
            lists) are silently dropped.

        Source is anchored at ``body.buyer.consent``: we never widen
        to the parent or to any sibling field, and we don't infer a
        consent shape from anywhere else. If consent is missing,
        non-dict, or yields no whitelisted flags after filtering,
        the column stays NULL.
        """
        if not isinstance(buyer, dict):
            return
        consent = buyer.get("consent")
        if not isinstance(consent, dict) or not consent:
            return
        filtered: Dict[str, bool] = {
            key: value
            for key, value in consent.items()
            if key in _BUYER_CONSENT_FIELDS and isinstance(value, bool)
        }
        if filtered:
            result["buyer_consent_json"] = json.dumps(filtered)

    @classmethod
    def _extract_discovery_payment(cls, payment: Any, result: Dict[str, Any]) -> None:
        """Extract payment handler info from the discovery profile.

        In the SDK, discovery responses place payment handlers at the
        top level as a sibling of ``ucp``::

            {"ucp": {...}, "payment": {"handlers": [...]}}

        This is separate from ``_extract_payment()`` which handles
        the ``payment`` object inside checkout/order responses.
        """
        if not isinstance(payment, dict):
            return
        handlers = payment.get("handlers")
        if not isinstance(handlers, list) or not handlers:
            return
        # Only set if _extract_payment hasn't already found an instrument
        if "payment_handler_id" not in result:
            first = handlers[0]
            if isinstance(first, dict):
                result["payment_handler_id"] = first.get("id") or first.get("name")

    @classmethod
    def _normalize_registry(cls, raw: Any) -> list:
        """Normalize capabilities to a flat list for analytics storage.

        The SDK/samples use an array format (primary)::

            [{"name": "dev.ucp.shopping.checkout", "version": "2026-01-11"}]

        Also handles an object-keyed dict format for robustness::

            {"dev.ucp.shopping.checkout": [{"version": "2026-01-11"}]}
        """
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            flat = []
            for domain_name, entries in raw.items():
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, dict):
                            item = {"name": domain_name, **entry}
                            flat.append(item)
                elif isinstance(entries, dict):
                    flat.append({"name": domain_name, **entries})
            return flat
        return []

    @classmethod
    def _extract_payment(cls, body: dict, result: Dict[str, Any]) -> None:
        """Extract payment fields from checkout/order responses.

        The SDK ``PaymentResponse`` contains both ``handlers[]`` (merchant
        configs) and ``instruments[]`` (buyer payment methods).  Instruments
        are preferred for analytics since they carry ``handler_id``, ``type``,
        and ``brand``.
        """
        payment = body.get("payment") or {}
        payment_data = body.get("payment_data") or {}

        # payment_data (from complete request/response)
        if isinstance(payment_data, dict) and payment_data:
            result["payment_handler_id"] = payment_data.get(
                "handler_id"
            ) or payment_data.get("id")
            result["payment_instrument_type"] = payment_data.get("type")
            result["payment_brand"] = payment_data.get("brand")
            return

        if not isinstance(payment, dict) or not payment:
            return

        # Spec format: payment.instruments[] (each instrument has handler_id)
        instruments = payment.get("instruments")
        if isinstance(instruments, list) and instruments:
            first = instruments[0]
            if isinstance(first, dict):
                result["payment_handler_id"] = first.get("handler_id") or first.get(
                    "id"
                )
                result["payment_instrument_type"] = first.get("type")
                result["payment_brand"] = first.get("brand")
            return

        # Legacy/demo format: payment.handlers[]
        handlers = payment.get("handlers")
        if isinstance(handlers, list) and handlers:
            first = handlers[0]
            if isinstance(first, dict):
                result["payment_handler_id"] = first.get("id")
                result["payment_instrument_type"] = first.get("type")
                result["payment_brand"] = first.get("brand")
            return

        # Direct fields
        result["payment_handler_id"] = payment.get("handler_id") or payment.get("id")
        result["payment_instrument_type"] = payment.get("type")
        result["payment_brand"] = payment.get("brand")

    @classmethod
    def _extract_fulfillment(cls, fulfillment: Any, result: Dict[str, Any]) -> None:
        """Extract fulfillment fields.

        Handles both checkout fulfillment (methods[]) and order fulfillment
        (expectations[]/events[]).
        """
        if not isinstance(fulfillment, dict):
            return

        # Checkout: fulfillment.methods[]
        methods = fulfillment.get("methods")
        if isinstance(methods, list) and methods:
            first = methods[0]
            if isinstance(first, dict):
                result["fulfillment_type"] = first.get("type")
                dests = first.get("destinations", [])
                if isinstance(dests, list) and dests:
                    dest = dests[0]
                    if isinstance(dest, dict):
                        # SDK: destination is a PostalAddress (direct fields)
                        # or has a nested address object
                        country = dest.get("address_country")
                        if not country:
                            addr = dest.get("address")
                            if isinstance(addr, dict):
                                country = addr.get("address_country")
                        result["fulfillment_destination_country"] = country
            return

        # Order: fulfillment.expectations[]
        expectations = fulfillment.get("expectations")
        if isinstance(expectations, list) and expectations:
            first = expectations[0]
            if isinstance(first, dict):
                result["fulfillment_type"] = first.get("method_type") or first.get(
                    "type"
                )
                dest = first.get("destination")
                if isinstance(dest, dict):
                    result["fulfillment_destination_country"] = dest.get(
                        "address_country"
                    )

    @classmethod
    def _extract_order_lifecycle(cls, body: dict, result: Dict[str, Any]) -> None:
        """Capture `fulfillment.events[]` + `adjustments[]` for orders.

        At UCP `order.md`/`c5c6139` the order has no top-level `status`.
        Lifecycle lives in two append-only arrays:

          * `fulfillment.events[]` — actual shipment events
            (processing, shipped, in_transit, delivered, failed_attempt,
            canceled, undeliverable, returned_to_sender). Per
            `fulfillment_event.json` each entry carries an `id`,
            `occurred_at` (RFC 3339), `type` (open string), `line_items[]`,
            and optional `tracking_*` / `carrier` / `description`.
          * `adjustments[]` — post-order events independent of fulfillment
            (refunds, returns, credits, price_adjustment, dispute,
            cancellation). Per `adjustment.json` each entry has an `id`,
            `type` (open string), `occurred_at`, `status`
            (pending/completed/failed), optional `line_items[]`,
            `totals[]`, `description`.

        We dump both arrays verbatim into JSON columns so dashboards
        can compute multi-event KPIs (e.g., time from shipped to
        delivered, refund-rate over time) directly via
        ``JSON_QUERY_ARRAY``. We also surface narrow "latest" scalars
        for the common "current state" pivot, picked by latest
        ``occurred_at``.
        """
        if not isinstance(body, dict):
            return

        fulfillment = body.get("fulfillment")
        if isinstance(fulfillment, dict):
            events = fulfillment.get("events")
            if isinstance(events, list) and events:
                # Filter to dicts to avoid serializing malformed entries
                # that would break JSON_QUERY downstream. We keep the
                # array length so analysts can see counts that include
                # the malformed entries if needed by walking
                # messages_json — but for this column we ship clean.
                clean = [e for e in events if isinstance(e, dict)]
                if clean:
                    result["fulfillment_events_json"] = json.dumps(clean, default=str)
                latest = _latest_by_occurred_at(events)
                if latest:
                    event_type = latest.get("type")
                    if isinstance(event_type, str) and event_type:
                        result["latest_fulfillment_event_type"] = event_type
                    # Only write the TIMESTAMP column when the value
                    # parses as a real RFC 3339 instant. The latest_*
                    # entry can come from the array-position fallback
                    # (no parseable timestamps in the array), in which
                    # case its raw `occurred_at` string would land in
                    # a BigQuery TIMESTAMP column and reject the entire
                    # row. Type / status columns still populate so the
                    # row stays useful even when timestamps are bad.
                    occurred_at = latest.get("occurred_at")
                    if (
                        isinstance(occurred_at, str)
                        and occurred_at
                        and _parse_occurred_at(occurred_at) is not None
                    ):
                        result["latest_fulfillment_event_at"] = occurred_at

        adjustments = body.get("adjustments")
        if isinstance(adjustments, list) and adjustments:
            clean = [a for a in adjustments if isinstance(a, dict)]
            if clean:
                result["adjustments_json"] = json.dumps(clean, default=str)
            latest = _latest_by_occurred_at(adjustments)
            if latest:
                adj_type = latest.get("type")
                if isinstance(adj_type, str) and adj_type:
                    result["latest_adjustment_type"] = adj_type
                adj_status = latest.get("status")
                if isinstance(adj_status, str) and adj_status:
                    result["latest_adjustment_status"] = adj_status
                occurred_at = latest.get("occurred_at")
                if (
                    isinstance(occurred_at, str)
                    and occurred_at
                    and _parse_occurred_at(occurred_at) is not None
                ):
                    result["latest_adjustment_at"] = occurred_at

    @classmethod
    def _extract_discounts(cls, discounts: Any, result: Dict[str, Any]) -> None:
        """Extract discount extension fields.

        Spec: discounts.codes (input), discounts.applied (output).
        """
        if not isinstance(discounts, dict):
            return

        codes = discounts.get("codes")
        if isinstance(codes, list) and codes:
            result["discount_codes_json"] = json.dumps(codes, default=str)

        applied = discounts.get("applied")
        if isinstance(applied, list) and applied:
            result["discount_applied_json"] = json.dumps(applied, default=str)
