from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from rental_automation.ledger import generate_monthly_ledgers
from rental_automation.models import Base, MonthlyLedger, Tenant


def test_monthly_ledger_carries_arrears_and_applies_advance_once():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(
            name="Dev Patel",
            unit_number="B-3",
            phone="+919999999999",
            monthly_rent=Decimal("10000"),
            maintenance_fee=Decimal("500"),
            opening_balance=Decimal("1500"),
            credit_balance=Decimal("2000"),
        )
        session.add(tenant)
        session.commit()

        october = generate_monthly_ledgers(session, date(2026, 10, 2))[0]
        assert october.arrears_brought_forward == Decimal("1500.00")
        assert october.amount_paid == Decimal("2000.00")
        assert october.net_due == Decimal("10000.00")
        assert october.status == "PARTIALLY_PAID"

        november = generate_monthly_ledgers(session, date(2026, 11, 2))[0]
        assert november.arrears_brought_forward == Decimal("10000.00")
        assert november.net_due == Decimal("20500.00")
        assert tenant.credit_balance == Decimal("0.00")

        same_month = generate_monthly_ledgers(session, date(2026, 11, 20))[0]
        assert same_month.id == november.id
        assert session.query(MonthlyLedger).count() == 2


def test_negative_opening_balance_is_applied_as_advance():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(
            name="Mira Shah",
            unit_number="C-8",
            phone="+919888888888",
            monthly_rent=Decimal("10000"),
            maintenance_fee=Decimal("500"),
            opening_balance=Decimal("-2500"),
        )
        session.add(tenant)
        session.commit()

        ledger = generate_monthly_ledgers(session, date(2026, 10, 2))[0]

        assert ledger.amount_paid == Decimal("2500.00")
        assert ledger.net_due == Decimal("8000.00")
        assert tenant.credit_balance == Decimal("0.00")

        same_month = generate_monthly_ledgers(session, date(2026, 10, 20))[0]
        assert same_month.id == ledger.id
        assert session.query(MonthlyLedger).count() == 1
