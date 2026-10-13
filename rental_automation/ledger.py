"""Monthly billing and tenant balance calculations."""

from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from rental_automation.models import LedgerStatus, MonthlyLedger, Tenant
from rental_automation.reconciliation import _ledger_status

ZERO = Decimal("0.00")


def month_start(value: date) -> date:
    """Normalize a date to the first day of its billing month."""
    return value.replace(day=1)


def calculate_ledger(
    tenant: Tenant,
    billing_month: date,
    arrears: Decimal,
    available_credit: Decimal,
) -> MonthlyLedger:
    """Calculate one month of charges, applying available credit exactly once."""
    rent = Decimal(tenant.monthly_rent)
    maintenance = Decimal(tenant.maintenance_fee)
    opening_arrears = max(arrears, ZERO)
    gross_due = rent + maintenance + opening_arrears
    applied_credit = min(max(available_credit, ZERO), gross_due)
    net_due = gross_due - applied_credit
    return MonthlyLedger(
        tenant_id=tenant.id,
        billing_month=month_start(billing_month),
        rent_billed=rent,
        maintenance_billed=maintenance,
        arrears_brought_forward=opening_arrears,
        amount_paid=applied_credit,
        net_due=net_due,
        status=_ledger_status(applied_credit, net_due),
    )


def generate_monthly_ledgers(
    session: Session, billing_month: date, *, dry_run: bool = False
) -> list[MonthlyLedger]:
    """Create each active tenant's ledger once, carrying forward latest balance."""
    billing_month = month_start(billing_month)
    tenants = list(session.scalars(select(Tenant).where(Tenant.is_active.is_(True))))
    generated: list[MonthlyLedger] = []
    for tenant in tenants:
        existing = session.scalar(
            select(MonthlyLedger).where(
                MonthlyLedger.tenant_id == tenant.id,
                MonthlyLedger.billing_month == billing_month,
            )
        )
        if existing:
            generated.append(existing)
            continue

        previous = session.scalar(
            select(MonthlyLedger)
            .where(
                MonthlyLedger.tenant_id == tenant.id,
                MonthlyLedger.billing_month < billing_month,
            )
            .order_by(MonthlyLedger.billing_month.desc())
            .limit(1)
        )
        opening_balance = Decimal(tenant.opening_balance)
        arrears = (
            Decimal(previous.net_due)
            if previous
            else max(opening_balance, ZERO)
        )
        available_credit = Decimal(tenant.credit_balance)
        if previous is None:
            available_credit += max(-opening_balance, ZERO)
        ledger = calculate_ledger(tenant, billing_month, arrears, available_credit)
        if not dry_run:
            tenant.credit_balance = available_credit - ledger.amount_paid
            session.add(ledger)
        generated.append(ledger)
    if not dry_run:
        session.commit()
    return generated


def reminder_deadline(billing_month: date, day: int) -> date:
    """Return a valid deadline in the billing month, clamping short months."""
    return billing_month.replace(day=min(day, monthrange(billing_month.year, billing_month.month)[1]))


def ledger_status_enum(status: str) -> LedgerStatus:
    """Convert a stored status to the public enum."""
    return LedgerStatus(status)
