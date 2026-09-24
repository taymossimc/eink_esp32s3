from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

import stripe


@dataclass(frozen=True)
class FundingReceipt:
    provider: str
    reference: str
    sandbox: bool


class FundingProvider(Protocol):
    name: str

    def authorize(self, organization_id: str, amount_minor: int, idempotency_key: str) -> FundingReceipt:
        ...


class DisabledFundingProvider:
    name = "disabled"

    def authorize(self, organization_id: str, amount_minor: int, idempotency_key: str) -> FundingReceipt:
        raise RuntimeError("No funding provider configured")


class DemoFundingProvider:
    """No-op sandbox provider. It never communicates with a payment network."""

    name = "demo"

    def authorize(self, organization_id: str, amount_minor: int, idempotency_key: str) -> FundingReceipt:
        value = f"{organization_id}:{amount_minor}:{idempotency_key}".encode()
        return FundingReceipt(
            provider="demo-local",
            reference="demo_pay_" + hashlib.sha256(value).hexdigest()[:24],
            sandbox=True,
        )


@dataclass(frozen=True)
class CheckoutSession:
    id: str
    url: str


class StripeFundingProvider:
    """Thin adapter around Stripe Checkout; card data remains on Stripe-hosted pages."""

    name = "stripe"

    def __init__(self, secret_key: str, webhook_secret: str, success_url: str, cancel_url: str):
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret
        self.success_url = success_url
        self.cancel_url = cancel_url

    def create_checkout(
        self,
        *,
        organization_id: str,
        user_id: str,
        pending_deposit_id: str,
        amount_minor: int,
        idempotency_key: str,
    ) -> CheckoutSession:
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                success_url=self.success_url.replace("{organization_id}", organization_id),
                cancel_url=self.cancel_url.replace("{organization_id}", organization_id),
                client_reference_id=pending_deposit_id,
                line_items=[
                    {
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": amount_minor,
                            "product_data": {"name": "TMAds prepaid campaign funds"},
                        },
                        "quantity": 1,
                    }
                ],
                metadata={
                    "organization_id": organization_id,
                    "user_id": user_id,
                    "pending_deposit_id": pending_deposit_id,
                },
                payment_intent_data={
                    "metadata": {
                        "organization_id": organization_id,
                        "user_id": user_id,
                        "pending_deposit_id": pending_deposit_id,
                    }
                },
                api_key=self.secret_key,
                idempotency_key=idempotency_key,
            )
        except stripe.StripeError as exc:
            raise RuntimeError("Stripe Checkout is temporarily unavailable") from exc
        if not session.url:
            raise RuntimeError("Stripe did not return a Checkout URL")
        return CheckoutSession(id=session.id, url=session.url)

    def retrieve_checkout(self, session_id: str) -> Any:
        try:
            return stripe.checkout.Session.retrieve(session_id, api_key=self.secret_key)
        except stripe.StripeError as exc:
            raise RuntimeError("Stripe Checkout is temporarily unavailable") from exc

    def construct_event(self, payload: bytes, signature: str) -> Any:
        if not self.webhook_secret:
            raise RuntimeError("Stripe webhook secret is not configured")
        return stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
