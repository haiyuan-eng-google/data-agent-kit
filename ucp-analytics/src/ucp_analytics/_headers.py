"""Header utilities shared by the tracker / middleware / HTTPX hook.

Lives in its own module so the HTTPX hook can use it without dragging in
the optional Starlette dependency that `middleware.py` imports at module
load time. Private (`_headers`) — not part of the package's public API.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json as _json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

# RFC 9421 §2.3 — Signature-Input is a Structured Field Dictionary whose
# values carry a `keyid` parameter as a quoted string. Multiple sig
# labels (sig1, sig2, …) can appear in the same header value separated
# by commas; for analytics we only need one `keyid` to answer
# "is this row signed and by what key", so we extract the first match.
_SIGNATURE_INPUT_KEYID_RE = re.compile(r';\s*keyid\s*=\s*"([^"]+)"')

# UCP `checkout-rest.md` documents UCP-Agent as an RFC 8941 Structured
# Field Dictionary with a `profile` member that is a quoted-string URI:
# `UCP-Agent: profile="https://platform.example/profile"`. We only need
# that one member, so a regex-anchored extraction beats pulling in a
# full RFC 8941 parser.
#
# Match only at the start of the header or after a `,` — RFC 8941
# Dictionary *members* are comma-separated, while `;` separates
# *parameters* attached to the preceding member. Allowing `;profile=...`
# would misattribute attacker-controlled parameter values, e.g.
# `foo="bar";profile="https://attacker.example"` would otherwise yield
# `https://attacker.example` as a valid profile URI.
_UCP_AGENT_PROFILE_RE = re.compile(r'(?:^|,)\s*profile\s*=\s*"([^"]+)"')


def lookup_header(headers: Optional[Mapping[str, str]], name: str) -> Optional[str]:
    """Case-insensitive header lookup.

    HTTP headers are case-insensitive per RFC 7230, but `dict(...)` of
    a Starlette / httpx headers object can leave casing in either form
    depending on the source. Walk the mapping ourselves so callers don't
    have to remember which casing the upstream framework normalized to.
    """
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == target:
            return value
    return None


def is_signed(headers: Optional[Mapping[str, str]]) -> bool:
    """True iff a complete RFC 9421 signature pair is present.

    UCP `signatures.md` requires both `Signature-Input` (metadata: covered
    components + keyid) and `Signature` (the signature value itself) to
    be present together. A half-signed exchange with only one of the two
    is malformed — counting it as signed would inflate the
    "% signed traffic" security KPI on every malformed request that ever
    reaches analytics.
    """
    sig_input = lookup_header(headers, "signature-input")
    sig = lookup_header(headers, "signature")
    return bool(sig_input and sig_input.strip() and sig and sig.strip())


def webhook_id(headers: Optional[Mapping[str, str]]) -> Optional[str]:
    """Return the value of the `Webhook-Id` header, or None.

    Standard Webhooks `Webhook-Id` (per UCP `order.md`) is the unique
    event identifier — useful for de-duping webhook deliveries and
    correlating analytics rows back to the merchant's outbound event.
    """
    raw = lookup_header(headers, "webhook-id")
    if not raw or not raw.strip():
        return None
    return raw.strip()


def webhook_timestamp_iso(
    headers: Optional[Mapping[str, str]],
) -> Optional[str]:
    """Parse `Webhook-Timestamp` (Unix seconds) into an ISO 8601 UTC string.

    UCP `order.md` documents `Webhook-Timestamp` as a *"Unix timestamp"*,
    not ISO 8601 — `datetime.fromisoformat(...)` would raise on every
    value and analytics would silently drop the column. We parse as
    seconds-since-epoch and emit a UTC ISO 8601 string suitable for a
    BigQuery `TIMESTAMP` column via `insert_rows_json`.

    Returns None if the header is absent or doesn't parse as an integer
    (rather than raising — analytics rows shouldn't fail on a single
    malformed sender).
    """
    raw = lookup_header(headers, "webhook-timestamp")
    if not raw or not raw.strip():
        return None
    try:
        seconds = int(raw.strip())
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def ucp_agent_profile_url(
    headers: Optional[Mapping[str, str]],
) -> Optional[str]:
    """Parse the `profile` member out of a UCP-Agent header.

    Per UCP `checkout-rest.md`, UCP-Agent is an RFC 8941 Structured Field
    Dictionary whose `profile` member is a quoted-string URI:

        UCP-Agent: profile="https://platform.example/profile"

    On a platform → business request, this is the platform's profile;
    on a business → platform webhook, it's the business's profile. The
    column name `ucp_agent_profile_url` is intentionally direction-
    neutral so a single column captures both cases — the existing
    `platform_profile_url` field is misleading on the webhook flow and
    is kept only for backwards compatibility.

    Returns the profile URI string if a UCP-Agent header is present and
    a `profile="..."` member can be parsed from it; otherwise None. We
    don't validate the URI itself — analytics records what was sent.
    """
    raw = lookup_header(headers, "ucp-agent")
    if not raw:
        return None
    match = _UCP_AGENT_PROFILE_RE.search(raw)
    return match.group(1) if match else None


# RFC 7235 / RFC 6750 / RFC 9728 — WWW-Authenticate Bearer challenge.
# Auth-params we care about for analytics: realm (always recommended),
# error, error_description, scope (RFC 6750 §3), and resource_metadata
# (RFC 9728).
#
# Both auth-scheme names and auth-param keys are RFC 7230 `token`s.
# `tchar` is much broader than \w — crucially it includes `-`, so
# hyphenated scheme names like `Mutual-Auth` or `New-Scheme` are
# valid. Using \w would misclassify hyphenated schemes as auth-param
# continuations of the prior challenge, leaking their params (e.g.
# `realm`) into the Bearer dict.
_TCHAR_RE = r"[A-Za-z0-9!#$%&'*+\-.^_`|~]"

# RFC 7235 §2.1 — auth-param = token BWS "=" BWS ( token / quoted-string )
# Both value forms are legal and senders use both. RFC 6750 §3's
# canonical examples ship `error` as a bare token (`error=invalid_token`,
# `error=insufficient_scope`) rather than a quoted string. A
# quoted-string-only regex silently drops those, leaving
# auth_challenge_error NULL on the most common challenge shape — and
# `error` is the failure-funnel dimension we most care about. The
# value branches into two capture groups: group 2 is the quoted form
# (with `[^"]*`-style content), group 3 is the bare-token form
# (tchar+); callers pick whichever matched.
_AUTH_PARAM_RE = re.compile(rf'({_TCHAR_RE}+)\s*=\s*(?:"([^"]*)"|({_TCHAR_RE}+))')

# Auth-scheme name token at the start of a challenge: a token followed
# by whitespace (params follow) or end-of-segment (token68 / no-params).
_SCHEME_TOKEN_RE = re.compile(rf"({_TCHAR_RE}+)(?:\s+|$)")


def _split_challenges(value: str) -> list:
    """Split a WWW-Authenticate value into (scheme, params_str) tuples.

    RFC 7235 §4.1 allows multiple challenges separated by commas, and
    auth-params within a challenge are also comma-separated — the
    disambiguator is that schemes are bare tokens while auth-params
    have ``=``. We do a quoted-string-aware comma split first, then
    classify each comma-separated segment as either a new challenge
    or an auth-param continuation of the previous challenge.

    Classification: a segment whose leading token is followed by
    ``=`` (with optional surrounding BWS per RFC 7230 §3.2.3) is an
    auth-param continuation; otherwise it is a new challenge. We
    can NOT key off "token + whitespace" alone — RFC 7235 §2.1 lets
    auth-params be written as ``token BWS "=" BWS value``, so
    forms like ``error = "invalid_token"`` would otherwise be
    misclassified as a new ``error`` scheme and dropped from the
    Bearer dict.

    Quoted strings are respected — a comma inside ``"..."`` is part of
    the value, not a separator. We don't fully implement RFC 7230
    backslash-escape handling inside quoted strings since UCP-shaped
    servers don't ship escaped quotes in auth-param values.
    """
    # Pre-pass: find comma positions that aren't inside a quoted string.
    in_quote = False
    comma_positions = []
    for i, c in enumerate(value):
        if c == '"':
            in_quote = not in_quote
        elif c == "," and not in_quote:
            comma_positions.append(i)
    starts = [0] + [c + 1 for c in comma_positions]
    ends = comma_positions + [len(value)]

    challenges: list = []
    current_scheme: Optional[str] = None
    current_params_segments: list = []
    for start, end in zip(starts, ends):
        segment = value[start:end].strip()
        if not segment:
            continue
        scheme_match = _SCHEME_TOKEN_RE.match(segment)
        is_new_challenge = False
        if scheme_match:
            # The token-followed-by-whitespace shape is ambiguous:
            # `Bearer realm="x"` is a new challenge, but
            # `error = "invalid_token"` is an auth-param continuation
            # because RFC 7235 §2.1 allows BWS around the `=`. Look
            # past any whitespace to the next non-space char — if it
            # is `=`, this is an auth-param, not a scheme.
            rest_after_token = segment[scheme_match.end() :].lstrip()
            if not rest_after_token.startswith("="):
                is_new_challenge = True
        if is_new_challenge:
            if current_scheme is not None:
                challenges.append((current_scheme, ", ".join(current_params_segments)))
            current_scheme = scheme_match.group(1)
            rest = segment[scheme_match.end() :].strip()
            current_params_segments = [rest] if rest else []
        else:
            # Auth-param continuation of the current challenge.
            if current_scheme is not None:
                current_params_segments.append(segment)
    if current_scheme is not None:
        challenges.append((current_scheme, ", ".join(current_params_segments)))
    return challenges


def parse_bearer_challenge(
    headers: Optional[Mapping[str, str]],
) -> Dict[str, str]:
    """Parse the auth-params of the first Bearer challenge in WWW-Authenticate.

    Returns a dict of ``{param_name: value}`` (lowercased keys) for the
    auth-params on the first Bearer scheme found in the header. Both
    value forms permitted by RFC 7235 §2.1 are accepted —
    ``auth-param = token BWS "=" BWS ( token / quoted-string )`` — so
    ``error=invalid_token`` (token) and
    ``realm="https://merchant.example"`` (quoted-string) both populate
    the dict. BWS around ``=`` is also accepted. Returns an empty dict
    if no ``WWW-Authenticate`` header is present or if it carries no
    Bearer challenge.

    Multi-challenge headers like
    ``Bearer realm="a", scope="b", Basic realm="c"`` are split on
    challenge boundaries first, so params from a following non-Bearer
    challenge can NOT leak into the Bearer dict. Pinned by tests:
    a Basic challenge alongside Bearer leaves Bearer's params
    untouched. Auth-scheme detection uses RFC 7230 ``token`` syntax
    (tchar+), so hyphenated schemes like ``Mutual-Auth`` or
    ``New-Scheme`` are correctly recognized as scheme tokens rather
    than swallowed as Bearer auth-param continuations.

    Spec refs:
      * RFC 7235 §2.1 — auth-param grammar (token / quoted-string, BWS)
      * RFC 7235 §4.1 — challenge syntax and the multi-challenge form
      * RFC 6750 §3 — Bearer auth-params (realm, error, error_description,
        scope)
      * RFC 9728 — resource_metadata pointer
    """
    raw = lookup_header(headers, "www-authenticate")
    if not raw:
        return {}
    for scheme, params_str in _split_challenges(raw):
        if scheme.lower() == "bearer":
            params: Dict[str, str] = {}
            for m in _AUTH_PARAM_RE.finditer(params_str):
                # Lowercase the key for case-insensitive lookups; the
                # spec treats auth-param names as case-insensitive.
                # Group 2 is the quoted-string value; group 3 is the
                # bare-token value. Exactly one of them is set per
                # match (the regex's outer group is `(?:"..."|tok)`).
                value = m.group(2) if m.group(2) is not None else m.group(3)
                params[m.group(1).lower()] = value
            return params
    return {}


def signature_keyid(headers: Optional[Mapping[str, str]]) -> Optional[str]:
    """Extract the first `keyid` parameter from `Signature-Input`.

    Returns the keyid string if a Signature-Input header is present and
    a `keyid="..."` parameter can be parsed from it; otherwise None. We
    only return one keyid even when the header carries multiple
    signature labels — analytics only needs one identifier to join
    against the JWK at `/.well-known/ucp`'s `signing_keys[]`.
    """
    raw = lookup_header(headers, "signature-input")
    if not raw:
        return None
    match = _SIGNATURE_INPUT_KEYID_RE.search(raw)
    return match.group(1) if match else None


# UCP `signatures.md` derives the signing algorithm from the matched
# JWK's `crv` field rather than the RFC 9421 `Signature-Input` params:
# *"The algorithm is derived from the key's `crv` field in the JWK;
# `alg` is NOT included in `Signature-Input` parameters"*. JWA names
# follow JWS conventions: P-256 + ECDSA + SHA-256 = ES256, P-384 +
# ECDSA + SHA-384 = ES384.
_CRV_TO_ALG = {
    "P-256": "ES256",
    "P-384": "ES384",
}


def decode_jose_header(credential: Any) -> Optional[Dict[str, Any]]:
    """Decode the JOSE header (first dot-separated segment) of a
    JWS / JWT / SD-JWT credential string.

    Decoding behavior is intentionally narrow: we ONLY decode the
    first segment (the JOSE header), NEVER the payload (the second
    segment) or any disclosures. The header carries non-secret
    metadata like ``alg``, ``kid``, ``typ`` that's useful for
    analytics; the payload carries privacy-sensitive claims about
    the buyer / merchant and must not be decoded or persisted.

    JOSE headers are base64url-encoded JSON per RFC 7515. RFC 7515
    §2 permits the encoder to omit ``=`` padding; we restore it
    before decoding so well-formed senders that strip padding still
    parse. Returns None on:
      * non-string input
      * no ``.`` in the string (not a JWS/JWT)
      * base64url decode failure
      * JSON parse failure
      * decoded value not a dict
    """
    if not isinstance(credential, str) or "." not in credential:
        return None
    header_b64 = credential.split(".", 1)[0]
    padded = header_b64 + "=" * (-len(header_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded)
        parsed = _json.loads(decoded)
    except (ValueError, TypeError, binascii.Error):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def credential_sha256(credential: Any) -> Optional[str]:
    """Hex SHA-256 of a credential string, treating it as opaque.

    Lets dashboards correlate the same credential across rows
    without persisting the credential itself. Returns None for
    non-string inputs so the caller's column stays NULL.
    """
    if not isinstance(credential, str) or not credential:
        return None
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def signature_alg_from_jwk(jwk: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Derive the JWS signing algorithm from a JWK.

    Returns the JWA algorithm name (``ES256`` / ``ES384``) for the
    JWK, or None when the curve isn't in the explicit mapping. UCP
    `signatures.md` requires the algorithm to be derived from `crv`;
    we do NOT fall back to the JWK's optional `alg` field on an
    unknown curve. The reason: `alg` is operator-controlled metadata
    that doesn't have to agree with the curve, so a fallback would
    misrepresent unsigned/wrong-curve cases as well-formed crypto.
    Future curve support (Ed25519 -> EdDSA, P-521 -> ES512, etc.)
    must be added as an explicit entry to ``_CRV_TO_ALG`` so the
    column stays NULL until each curve is reviewed.

    Returns None on a missing / non-dict / unknown-curve JWK so the
    caller's column stays NULL — preserving the three-state
    "we don't know the alg" semantic distinct from "we know it's
    ES256".
    """
    if not isinstance(jwk, Mapping):
        return None
    crv = jwk.get("crv")
    if isinstance(crv, str):
        return _CRV_TO_ALG.get(crv)
    return None
