"""Segment-aware path matching shared by the REST middleware and HTTPX hook.

Lives in its own module so the HTTPX hook can use it without dragging in
the optional Starlette dependency that `middleware.py` imports at module
load time. Private (`_path_match`) — not part of the package's public API.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional


def path_matches_marker(path: str, marker: str) -> bool:
    """Segment-aware match for a UCP path marker.

    True if `path` is exactly `marker`, has `marker` as a leading segment
    (`/marker/...`), has `marker` as the trailing segment under a mount
    (`/api/v1/marker`), or contains `marker` as an interior segment
    (`/api/v1/marker/{id}`). False for near-misses like `/api/catalogue/...`
    against `/catalog`, or `/api/orders-history` against `/orders`.
    """
    return (
        path == marker
        or path.startswith(marker + "/")
        or path.endswith(marker)
        or marker + "/" in path
    )


# Default UCP webhook path segments — the literal `/webhook` and
# `/webhooks` markers appear in `order.config.webhook_url` examples but
# the spec is explicit (UCP `order.md` at `c5c6139`):
#   "Businesses POST order events to a webhook URL provided by the
#    platform during partner onboarding. The URL format is
#    platform-specific."
# Real platforms ship `/events`, `/ucp-events`, `/hooks/<id>`, etc., so
# operators who run those need to extend this set via constructor
# `webhook_path_prefixes` on the middleware / HTTPX hook. The default
# stays narrow so the path filter doesn't over-capture on a plain UCP
# samples server.
_DEFAULT_WEBHOOK_PREFIXES = ("/webhook", "/webhooks")

# Known non-webhook UCP path segments. Header-based webhook detection
# is *suppressed* when the path matches one of these — the URL is the
# authoritative signal for those endpoints, and webhook headers
# stamped on (e.g.) `/checkout-sessions` can only come from a buggy
# or malicious sender. UCP `order.md` reserves Webhook-Id /
# Webhook-Timestamp for the Order Event Webhook flow specifically.
# Without this exclusion, a request to `/checkout-sessions` carrying
# stamped webhook headers would be misclassified as a webhook,
# corrupting de-dup / lag / correlation queries.
_NON_WEBHOOK_UCP_MARKERS = (
    "/checkout-sessions",
    "/carts",
    "/catalog",
    "/orders",
    "/identity",
    "/oauth2",
    "/.well-known/ucp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/testing/simulate",
    "/simulate-shipping",
)


def is_webhook_delivery(
    path: str,
    request_headers: Optional[Mapping[str, str]] = None,
    extra_prefixes: Iterable[str] = (),
) -> bool:
    """Decide whether an HTTP request is a UCP order-webhook delivery.

    Two independent signals satisfy detection:

    1. **Path-based** — the URL matches one of the default webhook
       prefixes (`/webhook`, `/webhooks`) or any operator-configured
       extension (e.g. `/events`, `/ucp-events`, `/hooks`).
    2. **Header-based** — Standard Webhooks ships `Webhook-Id` and
       `Webhook-Timestamp` together on every delivery. UCP `order.md`
       requires both for order-event webhooks. When both are present
       on a request whose path is *not* a known UCP REST marker,
       treat it as a webhook delivery regardless of URL — this
       catches `/events` and other platform-specific paths without
       the operator having to enumerate them.

    Header-based detection is suppressed on known non-webhook UCP
    paths (`/checkout-sessions`, `/carts`, `/catalog`, `/orders`,
    `/identity`, `/oauth2`, the `/.well-known/*` discovery endpoints,
    and the testing-simulate paths) so a request stamped with
    Webhook-Id / Webhook-Timestamp by a buggy or malicious sender
    can't override the URL — the URL is authoritative for those
    endpoints.

    Public via the `tracker` and `parser` modules; the helper itself
    stays in `_path_match` so middleware / HTTPX hook / classifier all
    share one definition of "webhook delivery."
    """
    prefixes = (*_DEFAULT_WEBHOOK_PREFIXES, *extra_prefixes)
    if any(path_matches_marker(path, p) for p in prefixes):
        return True

    # Don't fall through to header-based detection on known
    # non-webhook UCP paths — webhook headers there are spurious.
    if any(path_matches_marker(path, p) for p in _NON_WEBHOOK_UCP_MARKERS):
        return False

    if request_headers:
        target_id = "webhook-id"
        target_ts = "webhook-timestamp"
        has_id = False
        has_ts = False
        for key in request_headers.keys():
            if not isinstance(key, str):
                continue
            lower = key.lower()
            if lower == target_id:
                has_id = True
            elif lower == target_ts:
                has_ts = True
            if has_id and has_ts:
                return True

    return False
