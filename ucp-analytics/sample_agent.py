# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""sample_agent — reference shape for a UCP shopping agent's analytics
emission.

HTTPX response hooks (see ``ucp_analytics.UCPTracker.record``) capture
wire traffic; they can't see decisions an agent makes between requests.
``SampleAgent`` is the set of methods that cover the rest of the UCP
spec: payment outcomes, capability negotiation, payment-handler /
payment-instrument selection, and inbound webhook receipt processed
by a server-side handler.

Copy or subclass; nothing here is load-bearing beyond the
``record_event`` call.
"""

from __future__ import annotations

from typing import Optional

from ucp_analytics import UCPEvent, UCPTracker


class SampleAgent:
    """Reference shape for a UCP shopping agent's analytics emission.

    Each method is the spot in a real agent loop where you'd call
    ``tracker.record_event`` to log one of the agent-decision event
    types:

      * ``capability_negotiated`` — after parsing ``/.well-known/ucp``
        and picking which UCP feature set to use for this merchant.
      * ``payment_handler_negotiated`` — after the agent selects a
        payment handler (AP2 / network token / wallet) for the
        session.
      * ``payment_instrument_selected`` — when the user picks a card
        / wallet / saved instrument.
      * ``payment_completed`` / ``payment_failed`` — terminal payment
        outcomes the merchant returned (often piggybacked on
        checkout_session_completed, but worth emitting separately so
        payment dashboards don't have to dig into checkout bodies).
      * ``webhook_received`` — inbound webhook your server-side
        handler accepted (the HTTPX-side parser only sees outbound
        traffic; webhook delivery is inbound).
    """

    def __init__(self, tracker: UCPTracker) -> None:
        self.tracker = tracker

    async def capability_negotiated(
        self,
        *,
        merchant_host: Optional[str] = None,
        checkout_session_id: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="capability_negotiated",
            merchant_host=merchant_host,
            checkout_session_id=checkout_session_id,
        ))

    async def payment_handler_negotiated(
        self,
        *,
        checkout_session_id: Optional[str] = None,
        merchant_host: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="payment_handler_negotiated",
            checkout_session_id=checkout_session_id,
            merchant_host=merchant_host,
        ))

    async def payment_instrument_selected(
        self,
        *,
        checkout_session_id: Optional[str] = None,
        merchant_host: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="payment_instrument_selected",
            checkout_session_id=checkout_session_id,
            merchant_host=merchant_host,
        ))

    async def payment_completed(
        self,
        *,
        checkout_session_id: Optional[str] = None,
        order_id: Optional[str] = None,
        currency: Optional[str] = None,
        total_amount: Optional[int] = None,
        merchant_host: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="payment_completed",
            checkout_session_id=checkout_session_id,
            order_id=order_id,
            currency=currency,
            total_amount=total_amount,
            merchant_host=merchant_host,
        ))

    async def payment_failed(
        self,
        *,
        checkout_session_id: Optional[str] = None,
        merchant_host: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="payment_failed",
            checkout_session_id=checkout_session_id,
            merchant_host=merchant_host,
            error_code=error_code,
        ))

    async def webhook_received(
        self,
        *,
        order_id: Optional[str] = None,
        merchant_host: Optional[str] = None,
    ) -> None:
        await self.tracker.record_event(UCPEvent(
            event_type="order_webhook_received",
            order_id=order_id,
            merchant_host=merchant_host,
        ))
