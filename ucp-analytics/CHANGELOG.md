# Changelog

All notable changes to this project will be documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-11

Completes the alignment of the UCP analytics package with the UCP spec at
[`c5c6139`](https://github.com/Universal-Commerce-Protocol/ucp/commit/c5c6139)
(the identity-linking OAuth foundation merge). All concrete acceptance
rows from issue
[#8](https://github.com/haiyuan-eng-google/Universal-Commerce-Protocol-Analytics/issues/8)
are landed. Runtime Embedded Checkout postMessage events (`ec.totals.change`,
link delegation acceptance, reauth, cart binding) remain deferred until
host-side instrumentation exists.

### Added

#### Request / response observability
- `WWW-Authenticate` Bearer challenge parsing per RFC 7235 / 6750 / 9728 —
  surfaces `auth_challenge_realm` / `_error` / `_scope` /
  `_resource_metadata` for the failure-side identity-linking funnel.
  Multi-challenge / multi-line / BWS-around-`=` / token-form value /
  hyphenated-scheme cases are all spec-faithful.
- HTTP message signing per RFC 9421 / UCP `signatures.md`:
  `request_signed` / `response_signed` / `request_signature_keyid` /
  `response_signature_keyid` / `request_signature_alg` /
  `response_signature_alg`. Algorithm derived from JWK `crv` via an
  operator-provided `jwk_lookup` callable (P-256 → ES256, P-384 → ES384).
  Half-signed exchanges keep keyid extraction but skip alg derivation.
- Standard Webhooks metadata (UCP `order.md`): `webhook_id` and
  `webhook_timestamp` (Unix-seconds → ISO 8601 UTC), scoped to webhook
  flows only.
- `UCP-Agent` parsed as an RFC 8941 Structured Field Dictionary;
  `ucp_agent_profile_url` surfaces the `profile` member,
  direction-neutral.

#### Webhook detection
- `webhook_path_prefixes` constructor parameter on
  `UCPAnalyticsTracker` for platforms whose webhook URL isn't
  `/webhook(s)` (UCP `order.md`: *"The URL format is
  platform-specific"*).
- Standard Webhooks header-pair (`Webhook-Id` + `Webhook-Timestamp`)
  as a fallback signal — middleware and HTTPX hook accept the request
  and the classifier routes it through `ORDER_*` taxonomy.
- Header-fallback suppressed on known UCP REST paths
  (`/checkout-sessions`, `/carts`, `/catalog`, `/orders`, etc.) so
  malicious header-stamping can't override URL semantics.
- New `ORDER_WEBHOOK_RECEIVED` event type for webhook deliveries
  without recognizable lifecycle status, distinct from REST-driven
  `ORDER_UPDATED`.

#### Order lifecycle (c5c6139 schema)
- Lifecycle derivation from `fulfillment.events[]` and `adjustments[]`
  (the new c5c6139 shape; `order.json` no longer carries a top-level
  `status`).
- New columns: `fulfillment_events_json`, `adjustments_json`,
  `latest_fulfillment_event_type`, `latest_fulfillment_event_at`,
  `latest_adjustment_type`, `latest_adjustment_status`,
  `latest_adjustment_at`. Picked by RFC 3339-parsed `occurred_at`
  (handles mixed `Z` / `±HH:MM` offsets, rejects naive timestamps).
- New `ORDER_GET` event type for read-only `GET /orders/{id}` polls,
  distinct from `ORDER_UPDATED` PUT mutations.
- `order_label STRING` captured from order-shaped bodies only.

#### Totals
- `totals_json JSON` preserves the full ordered totals array verbatim
  (duplicates, `display_text`, `lines[]`, business-defined types per
  `total.json`'s open vocabulary).
- Per-type SUM aggregation across duplicate entries (split state+local
  tax, multi-line discount) so well-known scalar columns accumulate
  correctly instead of last-wins.
- New scalar columns for the new well-known types: `items_discount_amount`,
  `fulfillment_amount`, `fee_amount`.

#### Request-body Context
- `context_intent`, `context_language`, `context_currency`,
  `context_eligibility_json` from the UCP request-body Context object
  (preserved through the request/response merge).

#### Messages
- Per-severity code lists: `message_info_codes_json`,
  `message_warning_codes_json` (deduped, order-preserving).
- Three-state nullable convenience flags driven from `messages[].code`:
  - `identity_optional_present` (info-severity flag, PR #354 upstream).
  - `eligibility_accepted_present` / `eligibility_not_accepted_present` /
    `eligibility_invalid_present` — cross-severity capture per UCP
    `eligibility.md`; outcomes mutually exclusive in well-formed
    responses.

#### Embedded Checkout (server-observable slice)
- `embedded_delegations_json` and `embedded_color_schemes_json`
  unioned across embedded service entries in `/.well-known/ucp`
  discovery responses.
- `embedded_ec_color_scheme` from the request URL query parameter.
- Runtime postMessage events (`ec.totals.change`, link delegation
  acceptance, reauth, cart binding) — **deferred** until host
  instrumentation exists.

#### AP2 mandates + Buyer Consent (PII-safe + opt-in raw)
- Safe-by-default: `ap2_mandate_present`, `ap2_mandate_keys_json`,
  `ap2_mandate_metadata_json` (JOSE header only: `kid` / `alg` /
  `typ` + SHA-256 of the credential string, NEVER the payload),
  `buyer_consent_json` (strict whitelist of the four documented
  consent flags, boolean values only).
- Opt-in raw: `ap2_mandate_raw_json` gated on
  `include_ap2_raw=True`; credential field names force-included in
  redaction so values are always scrubbed.

#### Authorization signals (PII-safe + opt-in raw)
- Safe-by-default: `signals_present`, `signals_keys_json` (key names
  only, NEVER values).
- Opt-in raw: `signals_json` gated on `include_signals_raw=True`;
  `dev.ucp.buyer_ip` and `dev.ucp.user_agent` force-included in
  redaction. Operators can provide a custom `pii_fields` set
  (including the defaults if they want to preserve them) for
  additional reverse-domain PII signals; the force-included keys
  are always OR'd in afterward.

#### Payment discovery
- `payment_available_instruments_json` from
  `body.ucp.payment_handlers[*].available_instruments`, per-handler
  arrays preserved.

#### OAuth identity-linking
- Discovery paths classified for the OAuth identity-linking flow:
  `/.well-known/oauth-authorization-server`,
  `/.well-known/openid-configuration`,
  `/.well-known/oauth-protected-resource`, and `/oauth2/*` endpoints.

### Changed

- `_redact` handles non-string dict keys (int / None / tuple) without
  crashing on `.lower()`. Raw AP2 / signals capture filters non-string
  keys before JSON serialization since JSON object keys must be strings.
- Webhook detection precedence: webhook branch runs before the `/orders`
  REST branch so a platform publishing `/webhooks/orders` classifies as
  a webhook (more-specific signal) rather than as a REST `/orders`
  endpoint.
- Lifecycle taxonomy derives from response payload, not URL format —
  legacy URL-segment fallback (`/webhooks/order-delivered`) retained
  for back-compat.

### Notes

- Spec target: UCP `c5c6139` (2026-05-06).
- Deferred (out-of-scope for server-side analytics): Embedded
  Checkout runtime postMessage events.
- Test count: 488 passing.

## [0.1.0] — 2026-04-08

Initial release. UCP analytics tracker with FastAPI middleware, HTTPX
client hook, BigQuery writer, and ADK plugin. Supported UCP spec
version: `2026-04-08`.

[0.2.0]: https://github.com/haiyuan-eng-google/Universal-Commerce-Protocol-Analytics/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/haiyuan-eng-google/Universal-Commerce-Protocol-Analytics/releases/tag/v0.1.0
