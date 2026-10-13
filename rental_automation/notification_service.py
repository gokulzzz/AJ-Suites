"""Reminder message formatting and pluggable delivery adapters."""

import base64
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from rental_automation.config import Settings, get_settings
from rental_automation.ledger import reminder_deadline
from rental_automation.models import MonthlyLedger, Tenant

logger = logging.getLogger(__name__)


class NotificationAdapter(Protocol):
    """Transport contract for SMS, WhatsApp, or local mock delivery."""

    def send(self, phone: str, message: str) -> str: ...


class MockNotificationAdapter:
    """Log messages instead of sending them."""

    def send(self, phone: str, message: str) -> str:
        logger.info("MOCK notification to=%s message=%s", phone, message)
        return "mock"


class TwilioAdapter:
    """Send SMS or WhatsApp messages through the Twilio Messages API."""

    def __init__(self, settings: Settings, *, whatsapp: bool = False):
        if not all(
            (settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number)
        ):
            raise ValueError("Twilio account SID, auth token, and sender are required")
        self.settings = settings
        self.whatsapp = whatsapp

    def send(self, phone: str, message: str) -> str:
        sid = self.settings.twilio_account_sid
        sender = self.settings.twilio_from_number
        recipient = phone
        if self.whatsapp:
            sender = sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}"
            recipient = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        body = urlencode({"From": sender, "To": recipient, "Body": message}).encode()
        token = base64.b64encode(
            f"{sid}:{self.settings.twilio_auth_token}".encode()
        ).decode()
        request = Request(
            url,
            data=body,
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError) as exc:
            logger.exception("Twilio message delivery failed to=%s", phone)
            raise RuntimeError(f"Twilio delivery failed: {exc}") from exc


def build_notification_adapter(settings: Settings | None = None) -> NotificationAdapter:
    """Instantiate the configured delivery adapter."""
    settings = settings or get_settings()
    if settings.notification_backend == "twilio_sms":
        return TwilioAdapter(settings)
    if settings.notification_backend == "twilio_whatsapp":
        return TwilioAdapter(settings, whatsapp=True)
    return MockNotificationAdapter()


def upi_payment_uri(
    tenant: Tenant, amount: Decimal, payee_name: str, payee_vpa: str
) -> str | None:
    """Create a UPI intent payload suitable for use as a payment link or QR data."""
    from urllib.parse import quote

    if not payee_vpa:
        return None
    params = urlencode(
        {
            "pa": payee_vpa,
            "pn": payee_name,
            "am": f"{amount:.2f}",
            "cu": "INR",
            "tn": f"Rent {tenant.unit_number}",
        },
        quote_via=quote,
    )
    return f"upi://pay?{params}"


def format_reminder(
    tenant: Tenant,
    ledger: MonthlyLedger,
    deadline: date,
    payee_name: str,
    payee_vpa: str,
) -> str:
    """Render a concise reminder with a full bill breakdown and UPI payload."""
    payment_uri = upi_payment_uri(
        tenant, Decimal(ledger.net_due), payee_name, payee_vpa
    )
    payment_details = (
        f"UPI payment link/QR payload: {payment_uri}"
        if payment_uri
        else "Contact management for UPI payment details."
    )
    return (
        f"Hello {tenant.name}, rent reminder for {ledger.billing_month:%B %Y} "
        f"(Unit {tenant.unit_number}). Rent: INR {ledger.rent_billed:.2f}; "
        f"maintenance: INR {ledger.maintenance_billed:.2f}; arrears: "
        f"INR {ledger.arrears_brought_forward:.2f}; pending total: "
        f"INR {ledger.net_due:.2f}. Please pay by {deadline:%d %B %Y}. "
        f"{payment_details}"
    )


def send_monthly_reminders(
    session: Session,
    billing_month: date,
    *,
    dry_run: bool = False,
    adapter: NotificationAdapter | None = None,
    settings: Settings | None = None,
) -> list[tuple[int, str]]:
    """Preview or send reminders for unpaid ledgers, avoiding duplicate sends."""
    settings = settings or get_settings()
    adapter = adapter or build_notification_adapter(settings)
    month = billing_month.replace(day=1)
    ledgers = list(
        session.scalars(
            select(MonthlyLedger)
            .where(
                MonthlyLedger.billing_month == month,
                MonthlyLedger.net_due > 0,
            )
            .order_by(MonthlyLedger.tenant_id)
        )
    )
    outputs: list[tuple[int, str]] = []
    for ledger in ledgers:
        tenant = session.get(Tenant, ledger.tenant_id)
        if tenant is None:
            logger.error("Ledger %s refers to missing tenant %s", ledger.id, ledger.tenant_id)
            continue
        due_date = reminder_deadline(
            month, tenant.due_day or settings.reminder_deadline_day
        )
        message = format_reminder(
            tenant,
            ledger,
            due_date,
            settings.payment_upi_payee_name,
            settings.payment_upi_vpa,
        )
        outputs.append((tenant.id, message))
        if dry_run:
            continue
        if ledger.reminder_sent_at:
            logger.info("Reminder already sent tenant_id=%s month=%s", tenant.id, month)
            continue
        adapter.send(tenant.phone, message)
        if not isinstance(adapter, MockNotificationAdapter):
            ledger.reminder_sent_at = datetime.now().astimezone()
            session.commit()
    return outputs
