"""Direct unit coverage for the _headers helper.

Lives in its own file (no Starlette dependency) so the helper is
exercised even in dev-only environments where the optional [fastapi]
extra isn't installed.
"""

from __future__ import annotations

from ucp_analytics._headers import (
    credential_sha256,
    decode_jose_header,
    is_signed,
    lookup_header,
    parse_bearer_challenge,
    signature_alg_from_jwk,
    signature_keyid,
    ucp_agent_profile_url,
    webhook_id,
    webhook_timestamp_iso,
)


class TestLookupHeader:
    def test_exact_case(self):
        assert lookup_header({"Signature-Input": "x"}, "Signature-Input") == "x"

    def test_lowercase_input(self):
        assert lookup_header({"signature-input": "x"}, "Signature-Input") == "x"

    def test_uppercase_input(self):
        assert lookup_header({"SIGNATURE-INPUT": "x"}, "Signature-Input") == "x"

    def test_mixed_case(self):
        assert lookup_header({"sIgNaTuRe-InPuT": "x"}, "signature-input") == "x"

    def test_missing(self):
        assert lookup_header({"other": "y"}, "Signature-Input") is None

    def test_none_headers(self):
        assert lookup_header(None, "Signature-Input") is None

    def test_empty_headers(self):
        assert lookup_header({}, "Signature-Input") is None


class TestIsSigned:
    def test_both_signature_input_and_signature_present(self):
        # UCP signatures.md requires both headers to be present together;
        # only the pair is a real signature.
        headers = {
            "signature-input": (
                'sig1=("@method" "@path");keyid="key-1";created=1618884475'
            ),
            "signature": "sig1=:abc==:",
        }
        assert is_signed(headers) is True

    def test_pair_present_uppercase(self):
        # Real-world middleware can hand off either casing; both work.
        assert (
            is_signed(
                {
                    "Signature-Input": 'sig1=();keyid="k1"',
                    "Signature": "sig1=:abc==:",
                }
            )
            is True
        )

    def test_signature_input_only_is_not_signed(self):
        # Half-signed exchange: metadata header without the actual value.
        # Must NOT count as signed — counting it would inflate the
        # "% signed traffic" KPI on every malformed request.
        assert is_signed({"signature-input": 'sig1=();keyid="k1"'}) is False

    def test_signature_only_is_not_signed(self):
        # The other half: signature value without the metadata header that
        # advertises covered components and keyid.
        assert is_signed({"signature": "sig1=:abc==:"}) is False

    def test_absent(self):
        assert is_signed({"content-type": "application/json"}) is False

    def test_none(self):
        assert is_signed(None) is False

    def test_empty_signature_input_value(self):
        # Empty Signature-Input — not a real signature even if Signature
        # is somehow present.
        assert is_signed({"signature-input": "", "signature": "sig1=:abc==:"}) is False

    def test_empty_signature_value(self):
        assert (
            is_signed({"signature-input": 'sig1=();keyid="k1"', "signature": ""})
            is False
        )

    def test_whitespace_only_values(self):
        assert is_signed({"signature-input": "   ", "signature": "   "}) is False


class TestSignatureKeyid:
    def test_simple_keyid(self):
        headers = {
            "Signature-Input": (
                'sig1=("@method" "@path" "host");keyid="key-1";created=1618884475'
            )
        }
        assert signature_keyid(headers) == "key-1"

    def test_keyid_first_among_multiple_sig_labels(self):
        # RFC 9421 permits multiple signature labels; we take the first
        # parsable keyid for analytics.
        headers = {
            "Signature-Input": (
                'sig1=();keyid="key-A";alg="ecdsa-p256-sha256", sig2=();keyid="key-B"'
            )
        }
        assert signature_keyid(headers) == "key-A"

    def test_keyid_with_dotted_identifier(self):
        # Real UCP keyids look like JWK kids — alphanumeric / dashes /
        # dots / colons.
        headers = {
            "Signature-Input": ('sig1=();keyid="dev.merchant.example/key-2026-04-08"')
        }
        assert signature_keyid(headers) == "dev.merchant.example/key-2026-04-08"

    def test_no_keyid_parameter(self):
        # Header present but missing keyid (malformed, non-UCP, etc.).
        headers = {"Signature-Input": 'sig1=("@method")'}
        assert signature_keyid(headers) is None

    def test_no_signature_input_header(self):
        assert signature_keyid({"content-type": "application/json"}) is None

    def test_none(self):
        assert signature_keyid(None) is None

    def test_no_keyid_when_only_other_params_present(self):
        # `keyid` must be its own structured-field parameter; a header
        # with only other parameters (no keyid) returns None.
        assert signature_keyid({"Signature-Input": 'sig1=();foo="bar"'}) is None


class TestDecodeJoseHeader:
    """A4 — decode_jose_header decodes ONLY the first dot-separated
    segment (the JOSE header) of a JWS / JWT / SD-JWT credential.
    Never the payload, never the disclosures."""

    def test_decodes_standard_header(self):
        # base64url({"alg":"ES256","kid":"k1"})
        header = "eyJhbGciOiJFUzI1NiIsImtpZCI6ImsxIn0"
        # Detached JWS form: header..signature
        result = decode_jose_header(f"{header}..signature-data")
        assert result == {"alg": "ES256", "kid": "k1"}

    def test_decodes_with_padding_stripped(self):
        # RFC 7515 allows omitting `=` padding from base64url. Our
        # decoder restores it. Header here is 28 chars; raw base64
        # would need `=` padding to be a multiple of 4.
        header = "eyJhbGciOiJFUzI1NiIsImtpZCI6ImsxIn0"
        # Already 35 chars (no padding); decode should still work.
        result = decode_jose_header(f"{header}.payload.signature")
        assert result is not None
        assert result["alg"] == "ES256"

    def test_decodes_sd_jwt_header_with_typ(self):
        # base64url({"alg":"ES256","kid":"buyer","typ":"vc+sd-jwt"})
        header = "eyJhbGciOiJFUzI1NiIsImtpZCI6ImJ1eWVyIiwidHlwIjoidmMrc2Qtand0In0"
        # SD-JWT+kb shape: header.payload.sig~disclosures...
        result = decode_jose_header(f"{header}.payload.sig~d1~d2")
        assert result == {"alg": "ES256", "kid": "buyer", "typ": "vc+sd-jwt"}

    def test_none_input_returns_none(self):
        assert decode_jose_header(None) is None

    def test_non_string_returns_none(self):
        assert decode_jose_header(42) is None
        assert decode_jose_header(["a", "b"]) is None
        assert decode_jose_header({"already": "decoded"}) is None

    def test_empty_string_returns_none(self):
        assert decode_jose_header("") is None

    def test_no_dot_returns_none(self):
        # Not a JWS/JWT shape at all.
        assert decode_jose_header("just-a-string") is None

    def test_invalid_base64_returns_none(self):
        # First segment isn't valid base64url.
        assert decode_jose_header("!!!.payload.signature") is None

    def test_valid_base64_but_not_json_returns_none(self):
        # base64url of "hello world" decodes successfully but isn't JSON.
        import base64

        not_json = base64.urlsafe_b64encode(b"hello world").decode("ascii").rstrip("=")
        assert decode_jose_header(f"{not_json}.payload.sig") is None

    def test_valid_base64_json_but_not_dict_returns_none(self):
        # base64url of `[1,2,3]` — decodes to JSON but a list, not a dict.
        import base64

        encoded = base64.urlsafe_b64encode(b"[1,2,3]").decode("ascii").rstrip("=")
        assert decode_jose_header(f"{encoded}.payload.sig") is None


class TestCredentialSha256:
    """A4 — credential_sha256 computes a hex SHA-256 of the credential
    string treated as opaque. Lets dashboards correlate the same
    credential across rows without persisting the credential itself."""

    def test_hashes_string(self):
        import hashlib

        credential = "eyJhbGciOiJFUzI1NiJ9..sig"
        expected = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        assert credential_sha256(credential) == expected

    def test_none_returns_none(self):
        assert credential_sha256(None) is None

    def test_empty_string_returns_none(self):
        # Hashing the empty string is technically defined, but we
        # treat it as missing data — column stays NULL.
        assert credential_sha256("") is None

    def test_non_string_returns_none(self):
        assert credential_sha256(42) is None
        assert credential_sha256({"value": "x"}) is None
        assert credential_sha256(["a", "b"]) is None


class TestSignatureAlgFromJwk:
    """C5c — UCP signatures.md derives the signing algorithm from
    the JWK's `crv` field, NOT from `Signature-Input` parameters.
    `signature_alg_from_jwk` is the pure mapping helper that callers
    invoke after looking up the JWK by keyid."""

    def test_p256_curve_maps_to_es256(self):
        assert (
            signature_alg_from_jwk(
                {"kty": "EC", "crv": "P-256", "x": "...", "y": "..."}
            )
            == "ES256"
        )

    def test_p384_curve_maps_to_es384(self):
        assert (
            signature_alg_from_jwk(
                {"kty": "EC", "crv": "P-384", "x": "...", "y": "..."}
            )
            == "ES384"
        )

    def test_unknown_curve_returns_none_without_alg_fallback(self):
        # Curve isn't mapped and no `alg` fallback is present →
        # NULL on the analytics column. Distinguishes "we couldn't
        # determine alg" from "alg is ES256".
        assert (
            signature_alg_from_jwk(
                {"kty": "EC", "crv": "P-521", "x": "...", "y": "..."}
            )
            is None
        )

    def test_unknown_curve_returns_none_even_with_alg_field_present(self):
        # No fallback to the JWK's `alg` field on an unknown curve.
        # `alg` is operator-controlled metadata that doesn't have to
        # agree with the curve — a fallback would misrepresent
        # unsigned/wrong-curve cases as well-formed crypto. Future
        # curve support (Ed25519 -> EdDSA, P-521 -> ES512) must be
        # an explicit addition to _CRV_TO_ALG so the column stays
        # NULL until each curve is reviewed.
        assert (
            signature_alg_from_jwk(
                {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "x": "..."}
            )
            is None
        )
        assert (
            signature_alg_from_jwk({"kty": "EC", "crv": "P-521", "alg": "ES512"})
            is None
        )

    def test_known_curve_ignores_conflicting_alg(self):
        # Spec says derive from `crv` — if a JWK ships P-256 with
        # a conflicting `alg: HS256`, the curve wins.
        assert (
            signature_alg_from_jwk({"kty": "EC", "crv": "P-256", "alg": "HS256"})
            == "ES256"
        )

    def test_none_jwk_returns_none(self):
        assert signature_alg_from_jwk(None) is None

    def test_non_mapping_jwk_returns_none(self):
        # A flaky JWKS source might hand back a string / list /
        # other shape; we shouldn't crash.
        assert signature_alg_from_jwk("not-a-jwk") is None
        assert signature_alg_from_jwk(["EC", "P-256"]) is None

    def test_empty_jwk_returns_none(self):
        assert signature_alg_from_jwk({}) is None

    def test_non_string_crv_returns_none(self):
        assert signature_alg_from_jwk({"crv": 42}) is None


class TestWebhookId:
    def test_present(self):
        assert webhook_id({"Webhook-Id": "evt_abc123"}) == "evt_abc123"

    def test_lowercase(self):
        assert webhook_id({"webhook-id": "evt_xyz"}) == "evt_xyz"

    def test_strips_whitespace(self):
        assert webhook_id({"Webhook-Id": "  evt_abc  "}) == "evt_abc"

    def test_empty_value_returns_none(self):
        assert webhook_id({"Webhook-Id": ""}) is None
        assert webhook_id({"Webhook-Id": "   "}) is None

    def test_absent(self):
        assert webhook_id({"content-type": "application/json"}) is None

    def test_none(self):
        assert webhook_id(None) is None


class TestWebhookTimestampIso:
    """UCP order.md documents Webhook-Timestamp as Unix seconds, not
    ISO 8601. Parsing it as ISO 8601 (the obvious-but-wrong default)
    would silently drop the column on every webhook."""

    def test_unix_seconds_parses_to_iso_utc(self):
        # 2026-01-01T00:00:00Z = 1767225600
        result = webhook_timestamp_iso({"Webhook-Timestamp": "1767225600"})
        assert result == "2026-01-01T00:00:00+00:00"

    def test_lowercase_header_name(self):
        assert webhook_timestamp_iso({"webhook-timestamp": "1770000000"}) is not None

    def test_strips_whitespace(self):
        assert (
            webhook_timestamp_iso({"Webhook-Timestamp": "  1767225600  "})
            == "2026-01-01T00:00:00+00:00"
        )

    def test_iso_input_not_misparsed(self):
        # An ISO 8601 string in this header is malformed per spec.
        # Don't raise — return None so the row still flows.
        assert (
            webhook_timestamp_iso({"Webhook-Timestamp": "2026-01-01T00:00:00Z"}) is None
        )

    def test_garbage_returns_none(self):
        assert webhook_timestamp_iso({"Webhook-Timestamp": "not-a-number"}) is None

    def test_empty_value(self):
        assert webhook_timestamp_iso({"Webhook-Timestamp": ""}) is None
        assert webhook_timestamp_iso({"Webhook-Timestamp": "   "}) is None

    def test_absent(self):
        assert webhook_timestamp_iso({"content-type": "application/json"}) is None

    def test_none(self):
        assert webhook_timestamp_iso(None) is None

    def test_negative_unix_seconds_pre_epoch(self):
        # Pre-1970 timestamps are unusual but technically valid Unix
        # seconds. Parse without raising.
        result = webhook_timestamp_iso({"Webhook-Timestamp": "-86400"})
        assert result == "1969-12-31T00:00:00+00:00"

    def test_extreme_value_returns_none(self):
        # OverflowError / OSError on platform-out-of-range — return None
        # rather than raising, so a single bad sender doesn't crash a
        # row insert.
        assert (
            webhook_timestamp_iso({"Webhook-Timestamp": "999999999999999999999"})
            is None
        )


class TestUcpAgentProfileUrl:
    """UCP-Agent is an RFC 8941 Structured Field Dictionary with a
    `profile` member that's a quoted-string URI per checkout-rest.md."""

    def test_canonical_form(self):
        # The example from checkout-rest.md.
        assert (
            ucp_agent_profile_url(
                {"UCP-Agent": 'profile="https://platform.example/profile"'}
            )
            == "https://platform.example/profile"
        )

    def test_lowercase_header_name(self):
        assert (
            ucp_agent_profile_url(
                {"ucp-agent": 'profile="https://merchant.example/profile"'}
            )
            == "https://merchant.example/profile"
        )

    def test_uppercase_header_name(self):
        assert (
            ucp_agent_profile_url({"UCP-AGENT": 'profile="https://x.example/y"'})
            == "https://x.example/y"
        )

    def test_profile_with_other_dict_members(self):
        # RFC 8941 Dictionary members are comma-separated. `;` introduces
        # *parameters* on the preceding member, not new members.
        assert (
            ucp_agent_profile_url(
                {
                    "UCP-Agent": (
                        'profile="https://platform.example/profile",'
                        ' version="2026-04-08"'
                    )
                }
            )
            == "https://platform.example/profile"
        )

    def test_profile_after_other_member(self):
        # Order shouldn't matter; the `profile` member can appear after
        # an unrelated dictionary member, separated by a comma.
        assert (
            ucp_agent_profile_url(
                {
                    "UCP-Agent": (
                        'version="2026-04-08",'
                        ' profile="https://merchant.example/profile"'
                    )
                }
            )
            == "https://merchant.example/profile"
        )

    def test_profile_as_parameter_on_other_member_is_not_extracted(self):
        # In RFC 8941 syntax, `;profile="..."` is a parameter attached to
        # the preceding member, not a top-level member. A malformed /
        # malicious sender could otherwise smuggle an attacker-controlled
        # URI into our column via something like
        #   foo="bar";profile="https://attacker.example"
        # We must NOT extract this as a valid profile URI.
        assert (
            ucp_agent_profile_url(
                {"UCP-Agent": ('foo="bar";profile="https://attacker.example"')}
            )
            is None
        )
        assert (
            ucp_agent_profile_url(
                {
                    "UCP-Agent": (
                        'version="2026-04-08";profile="https://attacker.example"'
                    )
                }
            )
            is None
        )

    def test_no_profile_member(self):
        # Header present but missing the `profile` member (malformed,
        # non-UCP, etc.) — return None rather than misattributing.
        assert ucp_agent_profile_url({"UCP-Agent": 'version="2026-04-08"'}) is None

    def test_absent(self):
        assert ucp_agent_profile_url({"content-type": "application/json"}) is None

    def test_none(self):
        assert ucp_agent_profile_url(None) is None

    def test_empty_value(self):
        assert ucp_agent_profile_url({"UCP-Agent": ""}) is None

    def test_empty_profile_value_returns_none(self):
        # `profile=""` matches the regex but yields an empty URI which
        # isn't useful — but we don't currently special-case this; the
        # empty string lands in the column. Pin current behavior.
        assert ucp_agent_profile_url({"UCP-Agent": 'profile=""'}) is None

    def test_url_with_path_and_query(self):
        # Real profile URIs have paths and sometimes query strings.
        url = "https://platform.example/.well-known/ucp?v=2026-04-08"
        assert ucp_agent_profile_url({"UCP-Agent": f'profile="{url}"'}) == url


class TestParseBearerChallenge:
    """RFC 7235 / 6750 Bearer challenge parser. Returns a dict of
    auth-params extracted from the first Bearer scheme. Used by C10
    to populate auth_challenge_* analytics columns."""

    def test_full_challenge_with_all_params(self):
        # The shape RFC 9728 / OIDC-protected-resource issuers produce
        # on a 401 against a scoped UCP endpoint.
        header = (
            'Bearer realm="https://merchant.example",'
            ' error="insufficient_scope",'
            ' error_description="The access token requires the user_admin scope",'
            ' scope="dev.ucp.shopping.order:manage",'
            ' resource_metadata="https://merchant.example/.well-known/oauth-protected-resource"'
        )
        params = parse_bearer_challenge({"WWW-Authenticate": header})
        assert params["realm"] == "https://merchant.example"
        assert params["error"] == "insufficient_scope"
        assert (
            params["error_description"]
            == "The access token requires the user_admin scope"
        )
        assert params["scope"] == "dev.ucp.shopping.order:manage"
        assert (
            params["resource_metadata"]
            == "https://merchant.example/.well-known/oauth-protected-resource"
        )

    def test_realm_only_invalid_token(self):
        # Bare 401 with just a realm — common pre-OAuth-flow case.
        params = parse_bearer_challenge(
            {"WWW-Authenticate": 'Bearer realm="https://merchant.example"'}
        )
        assert params == {"realm": "https://merchant.example"}

    def test_invalid_token_error(self):
        params = parse_bearer_challenge(
            {
                "WWW-Authenticate": (
                    'Bearer realm="https://merchant.example", error="invalid_token"'
                )
            }
        )
        assert params["error"] == "invalid_token"

    def test_case_insensitive_scheme(self):
        # RFC 7235 treats the auth-scheme as case-insensitive.
        params = parse_bearer_challenge(
            {"www-authenticate": 'BEARER realm="x", error="invalid_token"'}
        )
        assert params == {"realm": "x", "error": "invalid_token"}

    def test_case_insensitive_param_keys(self):
        # The spec also treats auth-param names as case-insensitive;
        # we lowercase keys for stable downstream lookup.
        params = parse_bearer_challenge(
            {"WWW-Authenticate": 'Bearer Realm="x", ERROR="invalid_token"'}
        )
        assert params == {"realm": "x", "error": "invalid_token"}

    def test_basic_only_returns_empty(self):
        # No Bearer scheme present.
        params = parse_bearer_challenge({"WWW-Authenticate": 'Basic realm="merchant"'})
        assert params == {}

    def test_no_www_authenticate_header(self):
        assert parse_bearer_challenge({"content-type": "text/plain"}) == {}

    def test_none_headers(self):
        assert parse_bearer_challenge(None) == {}

    def test_empty_header_value(self):
        assert parse_bearer_challenge({"WWW-Authenticate": ""}) == {}

    def test_bearer_followed_by_basic_does_not_leak_basic_params(self):
        # Bearer is the first scheme in a multi-scheme value. Bearer's
        # params must be isolated — a following non-Bearer challenge's
        # params (here Basic's `realm`) must NOT leak into the Bearer
        # result. Without this, a sender that ships
        # `Bearer error="invalid_token", Basic realm="login"` would
        # corrupt the auth_challenge_realm column with the Basic realm
        # value, breaking the failure-side identity-linking funnel.
        params = parse_bearer_challenge(
            {"WWW-Authenticate": 'Bearer error="invalid_token", Basic realm="x"'}
        )
        assert params == {"error": "invalid_token"}
        assert "realm" not in params

    def test_bearer_with_realm_followed_by_basic(self):
        # Reviewer's repro from the C10 PR thread: Bearer with realm and
        # error, followed by Basic with its own realm. Bearer's realm
        # must win.
        params = parse_bearer_challenge(
            {
                "WWW-Authenticate": (
                    'Bearer realm="merchant", error="invalid_token", '
                    'Basic realm="login"'
                )
            }
        )
        assert params == {"realm": "merchant", "error": "invalid_token"}

    def test_bearer_after_basic_still_extracted(self):
        # Bearer doesn't have to be the first challenge.
        params = parse_bearer_challenge(
            {
                "WWW-Authenticate": (
                    'Basic realm="legacy", '
                    'Bearer realm="merchant", error="insufficient_scope"'
                )
            }
        )
        assert params == {
            "realm": "merchant",
            "error": "insufficient_scope",
        }

    def test_hyphenated_scheme_does_not_leak_params_into_bearer(self):
        # RFC 7235 auth-scheme is an RFC 7230 `token`, whose `tchar`
        # set includes `-`. A scheme like `New-Scheme` or `Mutual-Auth`
        # is therefore valid. With a `\w+`-only scheme detector, the
        # following challenge would be misclassified as an auth-param
        # continuation of Bearer and its `realm="other"` would
        # overwrite Bearer's `realm="merchant"`, corrupting the
        # auth_challenge_realm KPI on a 401 response. Reviewer's repro
        # from the C10 PR thread.
        params = parse_bearer_challenge(
            {"www-authenticate": ('Bearer realm="merchant", New-Scheme realm="other"')}
        )
        assert params == {"realm": "merchant"}

    def test_hyphenated_scheme_after_bearer_with_multiple_params(self):
        # Same shape as above but with more Bearer params, to make sure
        # the hyphenated-scheme boundary still terminates Bearer cleanly
        # rather than absorbing the trailing challenge as a pile of
        # extra continuations.
        params = parse_bearer_challenge(
            {
                "www-authenticate": (
                    'Bearer realm="merchant", error="invalid_token", '
                    'Mutual-Auth realm="other", token="x"'
                )
            }
        )
        assert params == {"realm": "merchant", "error": "invalid_token"}

    def test_bearer_after_hyphenated_scheme_still_extracted(self):
        # Bearer is the second challenge here; the first is hyphenated.
        # The hyphenated leading scheme must not eat the entire value.
        params = parse_bearer_challenge(
            {
                "www-authenticate": (
                    'Mutual-Auth realm="other", '
                    'Bearer realm="merchant", scope="dev.ucp.shopping.order:read"'
                )
            }
        )
        assert params == {
            "realm": "merchant",
            "scope": "dev.ucp.shopping.order:read",
        }

    def test_auth_param_with_bws_around_equals(self):
        # RFC 7235 §2.1: auth-param = token BWS "=" BWS ( token /
        # quoted-string ). BWS is "bad whitespace" — optional
        # whitespace permitted but discouraged, recipients MUST accept
        # it. A naive splitter that classifies any "token + whitespace"
        # segment as a new scheme would treat `error = "invalid_token"`
        # as a bogus `error` scheme and silently drop it from the Bearer
        # dict, underpopulating the most useful auth_challenge_*
        # columns on perfectly valid headers.
        params = parse_bearer_challenge(
            {
                "www-authenticate": (
                    'Bearer realm="merchant", error = "invalid_token", '
                    'scope="dev.ucp.shopping.order:manage"'
                )
            }
        )
        assert params == {
            "realm": "merchant",
            "error": "invalid_token",
            "scope": "dev.ucp.shopping.order:manage",
        }

    def test_resource_metadata_with_bws_around_equals(self):
        # The reviewer's other repro shape — `resource_metadata` is
        # the C10 column most commonly affected because the URL value
        # is long and senders that pretty-print headers often add
        # spaces around `=` for readability.
        params = parse_bearer_challenge(
            {
                "www-authenticate": (
                    'Bearer realm="merchant", '
                    "resource_metadata = "
                    '"https://merchant.example/.well-known/oauth-protected-resource"'
                )
            }
        )
        assert params == {
            "realm": "merchant",
            "resource_metadata": (
                "https://merchant.example/.well-known/oauth-protected-resource"
            ),
        }

    def test_bws_only_before_equals(self):
        # BWS is allowed on either side independently — pin the
        # asymmetric form.
        params = parse_bearer_challenge(
            {"www-authenticate": 'Bearer realm ="merchant", error= "invalid_token"'}
        )
        assert params == {"realm": "merchant", "error": "invalid_token"}

    def test_token_form_error_value(self):
        # RFC 7235 §2.1: auth-param value can be either token or
        # quoted-string. RFC 6750 §3's canonical examples ship `error`
        # as a bare token (`error=invalid_token`,
        # `error=insufficient_scope`) rather than a quoted string. A
        # quoted-only parser silently leaves auth_challenge_error NULL
        # on those — exactly the failure-funnel dimension we most care
        # about. Reviewer's repro for the C10 PR thread.
        params = parse_bearer_challenge(
            {
                "www-authenticate": (
                    'Bearer realm="merchant", error=invalid_token, scope="a b"'
                )
            }
        )
        assert params == {
            "realm": "merchant",
            "error": "invalid_token",
            "scope": "a b",
        }

    def test_token_form_error_with_bws(self):
        # Token-form value combined with BWS around `=` — the two
        # spec-permitted relaxations stacked together.
        params = parse_bearer_challenge(
            {
                "www-authenticate": (
                    'Bearer realm="merchant", error = insufficient_scope'
                )
            }
        )
        assert params == {
            "realm": "merchant",
            "error": "insufficient_scope",
        }

    def test_token_form_realm_and_quoted_scope(self):
        # All-token vs all-quoted edge: realm is bare token, scope is
        # quoted (must be — the value contains a space). Pin that
        # mixed forms within one challenge work.
        params = parse_bearer_challenge(
            {"www-authenticate": 'Bearer realm=merchant, scope="a b"'}
        )
        assert params == {"realm": "merchant", "scope": "a b"}

    def test_token_form_does_not_eat_following_challenge(self):
        # Bare-token value greediness must stop at non-tchar chars
        # (whitespace, comma). Without that, `error=invalid_token`
        # could swallow the trailing `, Basic realm="x"` and pollute
        # the Bearer dict.
        params = parse_bearer_challenge(
            {
                "www-authenticate": (
                    'Bearer realm="merchant", error=invalid_token, Basic realm="legacy"'
                )
            }
        )
        assert params == {"realm": "merchant", "error": "invalid_token"}

    def test_quoted_string_with_comma_not_split(self):
        # A comma inside a quoted-string value is part of the value,
        # not a separator. Without quote-aware splitting we'd treat
        # `"manage, read"` as ending the Bearer challenge.
        params = parse_bearer_challenge(
            {
                "WWW-Authenticate": (
                    'Bearer realm="merchant", '
                    'scope="dev.ucp.shopping.order:manage, read", '
                    'error="insufficient_scope"'
                )
            }
        )
        assert params["scope"] == "dev.ucp.shopping.order:manage, read"
        assert params["error"] == "insufficient_scope"

    def test_scope_with_multiple_oauth_scopes(self):
        # OAuth scopes are space-separated within a single quoted-string.
        header = (
            'Bearer realm="x", '
            'scope="dev.ucp.shopping.order:read dev.ucp.shopping.order:manage"'
        )
        params = parse_bearer_challenge({"WWW-Authenticate": header})
        assert (
            params["scope"]
            == "dev.ucp.shopping.order:read dev.ucp.shopping.order:manage"
        )
