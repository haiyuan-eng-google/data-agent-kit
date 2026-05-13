"""UCPAnalyticsTracker — the main entry point for recording UCP events.

Can be used directly (tracker.record()), or indirectly via the FastAPI
middleware or HTTPX event hook.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from ucp_analytics._headers import (
    is_signed,
    parse_bearer_challenge,
    signature_alg_from_jwk,
    signature_keyid,
    ucp_agent_profile_url,
    webhook_id,
    webhook_timestamp_iso,
)
from ucp_analytics._path_match import is_webhook_delivery
from ucp_analytics.events import UCPEvent
from ucp_analytics.parser import UCPResponseParser
from ucp_analytics.writer import AsyncBigQueryWriter

logger = logging.getLogger(__name__)


class UCPAnalyticsTracker:
    """Records UCP commerce events into BigQuery.

    Usage — direct::

        tracker = UCPAnalyticsTracker(project_id="my-proj")
        await tracker.record_http(
            method="POST",
            path="/checkout-sessions",
            status_code=201,
            request_body={...},
            response_body={...},
            latency_ms=142.5,
        )
        await tracker.close()

    Usage — FastAPI middleware::

        from ucp_analytics import UCPAnalyticsMiddleware
        app.add_middleware(UCPAnalyticsMiddleware, tracker=tracker)

    Usage — HTTPX client hook::

        from ucp_analytics import UCPClientEventHook
        client = httpx.AsyncClient(
            event_hooks={"response": [UCPClientEventHook(tracker)]}
        )
    """

    def __init__(
        self,
        project_id: str,
        dataset_id: str = "ucp_analytics",
        table_id: str = "ucp_events",
        *,
        app_name: str = "",
        batch_size: int = 50,
        auto_create_table: bool = True,
        redact_pii: bool = False,
        pii_fields: Optional[List[str]] = None,
        custom_metadata: Optional[Dict[str, str]] = None,
        webhook_path_prefixes: Optional[List[str]] = None,
        jwk_lookup: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
        include_ap2_raw: bool = False,
        include_signals_raw: bool = False,
    ):
        self.app_name = app_name
        self.redact_pii = redact_pii
        self.pii_fields = set(
            pii_fields
            or [
                "email",
                "phone",
                "first_name",
                "last_name",
                "phone_number",
                "street_address",
                "postal_code",
            ]
        )
        # A4: force-include AP2 credential field names in the
        # redaction set. These are cryptographic credentials
        # (detached JWS / SD-JWT+kb) that carry signed claims about
        # the buyer / merchant; they must never land in analytics
        # verbatim, even when `include_ap2_raw=True`. We OR these in
        # regardless of operator-provided pii_fields so a custom
        # pii_fields list can extend the redaction set but cannot
        # accidentally turn this safety off.
        self.pii_fields |= {"merchant_authorization", "checkout_mandate"}
        # A6: force-include known PII signal keys in the redaction
        # set. `dev.ucp.buyer_ip` (IP address) and
        # `dev.ucp.user_agent` (UA string) are documented PII
        # carriers per `signals.json` at c5c6139. Operators can
        # extend pii_fields with additional reverse-domain signal
        # keys for merchant-specific signals; the spec's defaults
        # are always redacted regardless of operator config.
        self.pii_fields |= {"dev.ucp.buyer_ip", "dev.ucp.user_agent"}
        # A4: include_ap2_raw=True surfaces the raw `body.ap2` object
        # into the `ap2_mandate_raw_json` column AFTER passing through
        # _redact (so credential strings are scrubbed). Disabled by
        # default — the safe-default columns (presence / keys / JOSE
        # metadata) are enough for KPI dashboards; raw is for
        # operators who need to forensically inspect mandate
        # structure.
        self.include_ap2_raw = include_ap2_raw
        # A6: include_signals_raw=True surfaces the raw `body.signals`
        # object into the `signals_json` column AFTER `_redact` so PII
        # signal values are scrubbed. Same pattern as include_ap2_raw:
        # disabled by default; safe-default columns
        # (`signals_present`, `signals_keys_json`) carry the
        # non-PII signal for everyone.
        self.include_signals_raw = include_signals_raw
        self.custom_metadata = custom_metadata
        # UCP order.md: "The URL format is platform-specific." The
        # default `/webhook(s)` prefix lives inside is_webhook_delivery;
        # operators on platforms that publish `/events`, `/ucp-events`,
        # `/hooks/<id>`, etc. extend that set here. The header-based
        # fallback (Webhook-Id + Webhook-Timestamp) catches deliveries
        # even when the path is unknown, so this list is a
        # noise-suppression knob more than a coverage knob.
        self.webhook_path_prefixes: tuple = tuple(webhook_path_prefixes or ())
        # C5c: jwk_lookup is an operator-provided callable that maps a
        # keyid to its JWK (the dict with `kty`, `crv`, optional `alg`,
        # etc.). UCP `signatures.md` derives the signing algorithm
        # from the JWK's `crv` field, not from `Signature-Input`, so
        # we need a way to reach the JWK. The callable shape lets
        # operators delegate to whatever JWK source they already have
        # (a cached /.well-known/ucp fetch, a Vault lookup, a static
        # rotation table, etc.). Defaults to None — when absent,
        # request_signature_alg / response_signature_alg stay NULL,
        # preserving the existing "signed: yes/no by keyid X" KPI.
        self.jwk_lookup: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = (
            jwk_lookup
        )

        self._writer = AsyncBigQueryWriter(
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            batch_size=batch_size,
            auto_create_table=auto_create_table,
        )
        self._pending_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # Primary API
    # ------------------------------------------------------------------ #

    async def record_http(
        self,
        *,
        method: str,
        url: str = "",
        path: str = "",
        status_code: int = 0,
        request_body: Optional[dict] = None,
        response_body: Optional[dict] = None,
        latency_ms: Optional[float] = None,
        request_headers: Optional[Dict[str, str]] = None,
        response_headers: Optional[Dict[str, str]] = None,
    ) -> UCPEvent:
        """Record a single UCP HTTP request/response pair.

        This is the core method called by the middleware / hooks.
        """
        headers = request_headers or {}

        # Resolve path and host from url
        parsed_url = urlparse(url) if url else None
        if not path and parsed_url:
            path = parsed_url.path

        merchant_host = (parsed_url.hostname or "") if parsed_url else ""

        # A3: extract embedded checkout query params from the request.
        # The `ec_color_scheme` param is sent by the host when fetching
        # the embedded checkout page to request a theme. parse_qs
        # returns lists (a query string can repeat a key); take the
        # first value to mirror what a web server would surface as
        # request.args["ec_color_scheme"]. Caps validity to "what was
        # sent" — operators may pass non-spec values and we record
        # signal fidelity.
        #
        # record_http accepts both `url` and `path`; direct callers
        # may pass the query string on either. Prefer the parsed url's
        # query when it exists; fall back to parsing the path itself
        # so `path="/embedded-checkout?ec_color_scheme=dark"` doesn't
        # silently drop the signal.
        query: str = ""
        if parsed_url and parsed_url.query:
            query = parsed_url.query
        elif path and "?" in path:
            query = urlparse(path).query
        embedded_ec_color_scheme: Optional[str] = None
        if query:
            params = parse_qs(query, keep_blank_values=False)
            values = params.get("ec_color_scheme")
            if values:
                embedded_ec_color_scheme = values[0]

        # Single source of truth for "is this a webhook delivery?" — used
        # both to gate body extraction toward the request side and to
        # scope Webhook-Id / Webhook-Timestamp capture to webhook flows
        # only. Per UCP order.md these headers belong to the Order Event
        # Webhook flow; capturing them off arbitrary requests would let
        # a buggy or malicious sender stamp webhook metadata onto a
        # checkout / cart / catalog row.
        #
        # is_webhook_delivery accepts either a default/configured path
        # prefix OR the Standard Webhooks header pair (Webhook-Id +
        # Webhook-Timestamp). The header pair is the strong signal --
        # UCP order.md requires both on every order-event webhook --
        # so a platform that publishes `/events` instead of `/webhooks`
        # is still detected without the operator having to enumerate
        # every variant.
        is_webhook = is_webhook_delivery(
            path, request_headers, self.webhook_path_prefixes
        )

        # Classify (pass request_body for webhook flows where payload
        # is in the request and response is just an ack). Forward the
        # same webhook-detection signals so the classifier picks the
        # ORDER_* taxonomy rather than falling through to REQUEST.
        event_type = UCPResponseParser.classify(
            method,
            path,
            status_code,
            response_body,
            request_body=request_body,
            request_headers=request_headers,
            webhook_path_prefixes=self.webhook_path_prefixes,
        )

        # Build event
        event = UCPEvent(
            event_type=event_type.value,
            app_name=self.app_name,
            merchant_host=merchant_host,
            http_method=method.upper(),
            http_path=path,
            http_status_code=status_code if status_code else None,
            latency_ms=latency_ms,
            platform_profile_url=headers.get("ucp-agent", ""),
            idempotency_key=headers.get("idempotency-key", ""),
            request_id=headers.get("request-id", ""),
            # HTTP message signing per RFC 9421. is_signed / signature_keyid
            # do their own case-insensitive lookup, so we don't need to
            # pre-normalize the header dict here.
            #
            # Distinguish "headers never observed" (None) from "headers
            # observed and unsigned" (False). Middleware and HTTPX hook
            # always pass a dict (possibly empty), so genuinely unsigned
            # traffic records False; direct callers that don't pass the
            # corresponding side record None — without this the
            # "% signed traffic" KPI would treat every direct-API row as
            # observed unsigned.
            request_signed=(
                is_signed(request_headers) if request_headers is not None else None
            ),
            response_signed=(
                is_signed(response_headers) if response_headers is not None else None
            ),
            request_signature_keyid=signature_keyid(request_headers),
            response_signature_keyid=signature_keyid(response_headers),
            # Standard Webhooks metadata (UCP order.md). The Webhook-*
            # headers ride on the inbound webhook *request* and are
            # scoped to the Order Event Webhook flow; gate on is_webhook
            # so a checkout / cart / catalog request that happens to
            # carry these headers (buggy sender, fuzzing, etc.) doesn't
            # stamp webhook metadata onto an unrelated row.
            # webhook_timestamp_iso parses the Unix-seconds value into
            # an ISO 8601 UTC string suitable for the TIMESTAMP column.
            webhook_id=webhook_id(request_headers) if is_webhook else None,
            webhook_timestamp=(
                webhook_timestamp_iso(request_headers) if is_webhook else None
            ),
            # UCP-Agent profile URI (RFC 8941 Dictionary, parsed). Direction-
            # neutral on purpose: on platform → business requests this is the
            # platform's profile, on business → platform webhooks it's the
            # business's. The legacy platform_profile_url field above keeps
            # storing the raw header string for backwards compatibility.
            ucp_agent_profile_url=ucp_agent_profile_url(request_headers),
            # A3: embedded checkout `ec_color_scheme` query param.
            # Surfaced unconditionally — the param is host-controlled
            # and the embedded checkout URL it travels on may not
            # match any UCP-specific path prefix; recording when it
            # appears tells dashboards which themes hosts are
            # actually requesting.
            embedded_ec_color_scheme=embedded_ec_color_scheme,
        )

        # WWW-Authenticate Bearer challenge (RFC 7235 / RFC 6750 / RFC 9728).
        # Lives on the response side — issued by the merchant on 401/403.
        # We parse whenever the challenge is present rather than gating on
        # status_code, which keeps the helper composable; senders that put
        # WWW-Authenticate on a non-failure response are technically out of
        # spec but we record what they sent rather than dropping data.
        challenge = parse_bearer_challenge(response_headers)
        if challenge:
            event.auth_challenge_error = challenge.get("error")
            event.auth_challenge_scope = challenge.get("scope")
            event.auth_challenge_realm = challenge.get("realm")
            event.auth_challenge_resource_metadata = challenge.get("resource_metadata")

        # C5c: signature algorithm per direction. UCP `signatures.md`:
        # *"The algorithm is derived from the key's `crv` field in the
        # JWK; `alg` is NOT included in `Signature-Input` parameters"*.
        # We need the keyid (already extracted above) plus a JWK
        # source — the operator-supplied jwk_lookup callable. Per
        # direction so request and response can be signed by different
        # parties using different keys / curves.
        #
        # Gated on `is_signed=True` (full Signature-Input + Signature
        # pair present). #12 intentionally extracts the keyid from
        # half-signed traffic for forensics — a request that ships
        # Signature-Input without Signature gets `request_signed=False`
        # and a non-null `request_signature_keyid`. Deriving the alg
        # for that case would make the column look like signed
        # crypto-agility data on an unsigned/incomplete exchange.
        # Keep keyid extraction unconditional; gate alg to fully
        # signed only.
        if self.jwk_lookup is not None:
            if event.request_signed:
                event.request_signature_alg = self._resolve_signature_alg(
                    event.request_signature_keyid
                )
            if event.response_signed:
                event.response_signature_alg = self._resolve_signature_alg(
                    event.response_signature_keyid
                )

        # Extract UCP fields from both request and response bodies.
        # Response takes precedence on conflict (it's the merchant-
        # confirmed state) but request-body-only fields — the new
        # context_intent / context_language / context_currency /
        # context_eligibility_json on a checkout-create or
        # catalog-search request, idempotency-related metadata, etc.
        # — survive even when the response has its own body.
        # For webhooks, the order payload is in the request body and
        # the response is just an ack like {"status": "ok"}, so we
        # extract only from the request body. (is_webhook computed
        # earlier; reused here.)
        if is_webhook:
            # Webhooks normally carry the order payload in the request
            # body, with the response being just an ack. Fall back to
            # response_body when the caller only has the response side
            # — matches the prior behavior so an
            # order_delivered classification doesn't end up with an
            # empty order_id / checkout_session_id.
            bodies_to_parse: List[Optional[dict]] = [request_body or response_body]
        else:
            bodies_to_parse = [request_body, response_body]
        for body in bodies_to_parse:
            if not body or not isinstance(body, dict):
                continue

            # A4: AP2 mandate metadata + buyer consent extract from
            # the ORIGINAL (un-redacted) body. The safe-default
            # outputs (presence / key-names / SHA-256 / JOSE header
            # fields / consent flags) are non-PII by construction.
            # Running them after self._redact() would corrupt
            # SHA-256 with the hash of "[REDACTED]" and lose the
            # JOSE header decode, defeating the columns.
            ap2_fields: Dict[str, Any] = {}
            UCPResponseParser._extract_ap2_mandate(body.get("ap2"), ap2_fields)
            UCPResponseParser._extract_buyer_consent(body.get("buyer"), ap2_fields)
            for key, val in ap2_fields.items():
                if hasattr(event, key):
                    setattr(event, key, val)

            # A4: opt-in raw AP2 capture. Always passes through
            # _redact regardless of self.redact_pii — pii_fields
            # includes the credential field names by construction so
            # `merchant_authorization` / `checkout_mandate` strings
            # are scrubbed before serializing. The capture is gated
            # on `include_ap2_raw` only; operators who want forensic
            # mandate inspection opt in, everyone else gets NULL.
            #
            # Filter to string keys before json.dumps: JSON object
            # keys must be strings, and tuple / object keys would
            # raise TypeError. Matches the parser safe-path behavior
            # which also drops non-string keys.
            if self.include_ap2_raw:
                ap2_obj = body.get("ap2")
                if isinstance(ap2_obj, dict):
                    clean_ap2 = {k: v for k, v in ap2_obj.items() if isinstance(k, str)}
                    if clean_ap2:
                        event.ap2_mandate_raw_json = json.dumps(
                            self._redact(clean_ap2), default=str
                        )

            # A6: opt-in raw signals capture. Same pattern as AP2.
            # pii_fields includes `dev.ucp.buyer_ip` and
            # `dev.ucp.user_agent` by construction so values for the
            # documented PII signals are scrubbed; operators who
            # ship additional reverse-domain PII signals can extend
            # via the `pii_fields` constructor parameter. Non-string
            # keys are filtered for the same JSON-serialization
            # reason as AP2.
            if self.include_signals_raw:
                signals_obj = body.get("signals")
                if isinstance(signals_obj, dict):
                    clean_signals = {
                        k: v for k, v in signals_obj.items() if isinstance(k, str)
                    }
                    if clean_signals:
                        event.signals_json = json.dumps(
                            self._redact(clean_signals), default=str
                        )

            if self.redact_pii:
                body = self._redact(body)
            fields = UCPResponseParser.extract(body)
            for key, val in fields.items():
                if hasattr(event, key):
                    setattr(event, key, val)

        # Attach custom metadata
        if self.custom_metadata:
            event.custom_metadata_json = json.dumps(self.custom_metadata)

        await self._writer.enqueue(event.to_bq_row())
        return event

    async def record_jsonrpc(
        self,
        *,
        tool_name: str,
        transport: str = "mcp",
        status_code: int = 200,
        response_body: Optional[dict] = None,
        latency_ms: Optional[float] = None,
        merchant_host: str = "",
    ) -> UCPEvent:
        """Record a JSON-RPC (MCP or A2A) event.

        Maps tool/action names to UCP event types via classify_jsonrpc(),
        then extracts fields from the response body.
        """
        event_type = UCPResponseParser.classify_jsonrpc(
            tool_name, status_code, response_body
        )

        # Look up HTTP equivalent for metadata
        http_mapping = UCPResponseParser._TOOL_TO_HTTP.get(tool_name, ("", ""))
        method, path = http_mapping

        event = UCPEvent(
            event_type=event_type.value,
            app_name=self.app_name,
            merchant_host=merchant_host,
            transport=transport,
            http_method=method.upper() if method else "",
            http_path=path,
            http_status_code=status_code if status_code else None,
            latency_ms=latency_ms,
        )

        if response_body and isinstance(response_body, dict):
            if self.redact_pii:
                response_body = self._redact(response_body)
            fields = UCPResponseParser.extract(response_body)
            for key, val in fields.items():
                if hasattr(event, key):
                    setattr(event, key, val)

        if self.custom_metadata:
            event.custom_metadata_json = json.dumps(self.custom_metadata)

        await self._writer.enqueue(event.to_bq_row())
        return event

    async def record_event(self, event: UCPEvent) -> None:
        """Record a manually constructed event."""
        await self._writer.enqueue(event.to_bq_row())

    async def flush(self):
        """Force flush buffered events to BigQuery."""
        await self._writer.flush()

    def _resolve_signature_alg(self, keyid: Optional[str]) -> Optional[str]:
        """Resolve a keyid to its JWA algorithm via the configured lookup.

        Returns None on any failure path so the column stays NULL —
        we explicitly distinguish "no alg recorded" from "wrong alg":
          * `keyid` missing → no signature to resolve
          * `jwk_lookup` not configured → operator opted out
          * `jwk_lookup` returned None → unknown keyid
          * JWK returned but `crv` doesn't map and no `alg` fallback
            → unknown curve

        Lookup callable errors are swallowed and logged; a flaky JWKS
        source must not take down analytics rows.
        """
        if not keyid or self.jwk_lookup is None:
            return None
        try:
            jwk = self.jwk_lookup(keyid)
        except Exception:
            logger.exception("UCP analytics jwk_lookup failed for keyid=%s", keyid)
            return None
        return signature_alg_from_jwk(jwk)

    def register_pending_task(self, task: asyncio.Task) -> None:
        """Track a fire-and-forget task (used by middleware).

        Tasks are automatically removed when done.  Call
        :meth:`drain_pending` (or :meth:`close`, which calls it) to
        await all in-flight tasks before shutdown.
        """
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def drain_pending(self) -> None:
        """Await all in-flight recording tasks.

        The FastAPI middleware fires analytics recording as background
        tasks so it doesn't block the HTTP response.  Call this before
        :meth:`close` — or just call :meth:`close`, which drains
        automatically.
        """
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()

    async def close(self):
        """Drain pending tasks, flush, and release resources."""
        await self.drain_pending()
        await self._writer.close()
        logger.info("UCPAnalyticsTracker closed")

    # ------------------------------------------------------------------ #
    # PII redaction
    # ------------------------------------------------------------------ #

    def _redact(self, data: Any) -> Any:
        if isinstance(data, dict):
            # Non-string keys can appear in malformed senders' payloads
            # (e.g. int / None / tuple keys); they can't match
            # `pii_fields` (which holds strings) and `.lower()` would
            # raise AttributeError on them. Guard with isinstance —
            # non-string keys keep their value unchanged but the value
            # is still walked recursively in case it contains nested
            # PII keys.
            return {
                k: (
                    "[REDACTED]"
                    if isinstance(k, str) and k.lower() in self.pii_fields
                    else self._redact(v)
                )
                for k, v in data.items()
            }
        if isinstance(data, list):
            return [self._redact(item) for item in data]
        return data
