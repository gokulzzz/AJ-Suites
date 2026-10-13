from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from rental_automation.models import Base, MonthlyLedger, PaymentTransaction, Tenant
from rental_automation.reconciliation import IncomingPayment, reconcile_payments


def make_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def tenant(session: Session) -> Tenant:
    record = Tenant(
        name="Asha Rao",
        unit_number="A-12",
        phone="+919876543210",
        upi_vpa="asha@okbank",
        monthly_rent=Decimal("12000"),
        maintenance_fee=Decimal("500"),
    )
    session.add(record)
    session.commit()
    return record


def test_matches_vpa_and_reconciles_idempotently():
    with make_session() as session:
        expected = tenant(session)
        payment = IncomingPayment(
            amount=Decimal("5000"),
            timestamp=datetime(2026, 10, 2, 8, 0),
            bank_reference_id="UTR123456789",
            source_transaction_id="UTR123456789",
            sender_vpa="ASHA@OKBANK",
        )

        first = reconcile_payments(session, [payment])
        second = reconcile_payments(session, [payment])

        assert first[0].status == "MATCHED"
        assert first[0].tenant_id == expected.id
        assert second[0].status == "DUPLICATE"
        assert session.scalar(select(PaymentTransaction).where(
            PaymentTransaction.bank_reference_id == "UTR123456789"
        )) is not None
        assert session.scalar(select(PaymentTransaction.id).where(
            PaymentTransaction.bank_reference_id == "UTR123456789"
        )) is not None


def test_payment_reduces_latest_ledger_and_carries_excess_as_credit():
    with make_session() as session:
        expected = tenant(session)
        ledger = MonthlyLedger(
            tenant_id=expected.id,
            billing_month=datetime(2026, 10, 1).date(),
            rent_billed=Decimal("12000"),
            maintenance_billed=Decimal("500"),
            arrears_brought_forward=Decimal("1000"),
            amount_paid=Decimal("0"),
            net_due=Decimal("13500"),
            status="OVERDUE",
        )
        session.add(ledger)
        session.commit()

        reconcile_payments(session, [
            IncomingPayment(
                amount=Decimal("14000"),
                timestamp=datetime(2026, 10, 4),
                bank_reference_id="UTR987654321",
                sender_vpa="asha@okbank",
            )
        ])

        session.refresh(ledger)
        session.refresh(expected)
        assert ledger.net_due == Decimal("0.00")
        assert ledger.status == "PAID"
        assert expected.credit_balance == Decimal("500.00")
