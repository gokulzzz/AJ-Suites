"""Statement ingestion, tenant matching, and idempotent payment posting."""

import csv
import difflib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Protocol

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rental_automation.config import get_settings
from rental_automation.models import MonthlyLedger, PaymentTransaction, Tenant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingPayment:
    """Normalized credit transaction supplied by an ingestion adapter."""

    amount: Decimal
    timestamp: datetime
    bank_reference_id: str | None = None
    source_transaction_id: str | None = None
    payment_mode: str = "UPI"
    sender_vpa: str | None = None
    sender_name: str | None = None
    remarks: str | None = None


class PaymentSourceAdapter(Protocol):
    """Contract implemented by statement, API, SMS, and email adapters."""

    def read(self, source: Path) -> Iterable[IncomingPayment]: ...


class StatementFileAdapter:
    """Read CSV and XLSX bank credits using common statement column names."""

    ALIASES = {
        "amount": ("amount", "credit", "deposit", "credit amount"),
        "timestamp": ("timestamp", "date", "transaction date", "value date"),
        "reference": ("bank_reference_id", "utr", "rrn", "reference", "txn id"),
        "sender_vpa": ("sender_vpa", "upi id", "vpa", "payer vpa"),
        "sender_name": ("sender_name", "remitter", "payer", "name"),
        "remarks": ("remarks", "narration", "description", "particulars"),
        "payment_mode": ("payment_mode", "mode", "transaction type"),
    }

    def read(self, source: Path) -> Iterable[IncomingPayment]:
        suffix = source.suffix.lower()
        if suffix == ".csv":
            with source.open("r", encoding="utf-8-sig", newline="") as file:
                yield from self._rows(csv.DictReader(file))
            return
        if suffix in {".xlsx", ".xlsm"}:
            try:
                from openpyxl import load_workbook
            except ImportError as exc:
                raise RuntimeError("Install openpyxl to import XLSX statements") from exc
            workbook = load_workbook(source, read_only=True, data_only=True)
            try:
                sheet = workbook.active
                values = sheet.iter_rows(values_only=True)
                headers = next(values, None)
                if not headers:
                    return
                yield from self._rows(
                    [dict(zip(headers, row, strict=False)) for row in values]
                )
            finally:
                workbook.close()
            return
        raise ValueError(f"Unsupported statement format: {suffix}")

    def _rows(self, rows: Iterable[dict]) -> Iterable[IncomingPayment]:
        for row in rows:
            normalized = {
                str(key).strip().lower(): value for key, value in row.items() if key
            }
            fields = {
                name: self._first(normalized, aliases)
                for name, aliases in self.ALIASES.items()
            }
            amount = self._amount(fields["amount"])
            if amount <= 0:
                continue
            timestamp = self._timestamp(fields["timestamp"])
            yield IncomingPayment(
                amount=amount,
                timestamp=timestamp,
                bank_reference_id=self._text(fields["reference"]),
                source_transaction_id=self._text(fields["reference"]),
                payment_mode=(self._text(fields["payment_mode"]) or "UPI").upper(),
                sender_vpa=self._text(fields["sender_vpa"]),
                sender_name=self._text(fields["sender_name"]),
                remarks=self._text(fields["remarks"]),
            )

    @staticmethod
    def _first(row: dict, aliases: tuple[str, ...]):
        for alias in aliases:
            if alias in row and row[alias] not in (None, ""):
                return row[alias]
        return None

    @staticmethod
    def _text(value) -> str | None:
        return str(value).strip() if value not in (None, "") else None

    @staticmethod
    def _amount(value) -> Decimal:
        if value in (None, ""):
            raise ValueError("Statement row is missing transaction amount")
        try:
            amount = Decimal(str(value).replace(",", "").replace("₹", "").strip())
        except InvalidOperation as exc:
            raise ValueError(f"Invalid transaction amount: {value!r}") from exc
        return amount

    @staticmethod
    def _timestamp(value) -> datetime:
        if isinstance(value, datetime):
            return value
        if value is None or value == "":
            raise ValueError("Statement row is missing transaction timestamp")
        if hasattr(value, "to_pydatetime"):
            return value.to_pydatetime()
        text = str(value).strip()
        for format_string in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y",
        ):
            try:
                return datetime.strptime(text, format_string)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Invalid transaction timestamp: {value!r}") from exc


class AlertTextAdapter:
    """Parse common UPI/IMPS transaction alert text lines."""

    AMOUNT = re.compile(r"(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
    REFERENCE = re.compile(
        r"(?:UTR|RRN|Ref(?:erence)?(?:\s*(?:No|ID))?)\s*[:#-]?\s*([A-Z0-9]{6,})",
        re.I,
    )
    VPA = re.compile(r"\b[\w.\-]+@[\w.\-]+\b")

    def parse(self, text: str, timestamp: datetime) -> IncomingPayment:
        """Parse a single alert; raises ValueError when no credit amount is present."""
        amount_match = self.AMOUNT.search(text)
        if not amount_match:
            raise ValueError("Alert does not contain a recognizable INR amount")
        ref_match = self.REFERENCE.search(text)
        vpa_match = self.VPA.search(text)
        return IncomingPayment(
            amount=Decimal(amount_match.group(1).replace(",", "")),
            timestamp=timestamp,
            bank_reference_id=ref_match.group(1) if ref_match else None,
            source_transaction_id=ref_match.group(1) if ref_match else None,
            sender_vpa=vpa_match.group(0) if vpa_match else None,
            remarks=text,
        )


@dataclass(frozen=True)
class ReconciliationResult:
    """Outcome for one imported payment."""

    reference: str | None
    tenant_id: int | None
    status: str
    amount: Decimal


def match_tenant(payment: IncomingPayment, tenants: Iterable[Tenant]) -> Tenant | None:
    """Match by exact expected identifiers first, then conservative fuzzy text."""
    candidates = list(tenants)
    if payment.sender_vpa:
        normalized_vpa = payment.sender_vpa.casefold()
        exact_matches = [
            tenant
            for tenant in candidates
            if (
                tenant.upi_vpa
                and tenant.upi_vpa.casefold() == normalized_vpa
            )
            or any(
                identifier.kind.casefold() in {"vpa", "upi", "upi_vpa"}
                and identifier.value.casefold() == normalized_vpa
                for identifier in tenant.identifiers
            )
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if exact_matches:
            return None

    searchable = [
        text
        for text in (payment.sender_name, payment.remarks)
        if text
    ]
    settings = get_settings()
    scores: dict[int, tuple[float, Tenant]] = {}
    for tenant in candidates:
        expected = [tenant.name, tenant.unit_number]
        expected.extend(identifier.value for identifier in tenant.identifiers)
        tenant_score = 0.0
        for candidate_text in searchable:
            normalized = candidate_text.casefold()
            for expected_text in expected:
                expected_normalized = expected_text.casefold()
                ratio = difflib.SequenceMatcher(
                    None, expected_normalized, normalized
                ).ratio()
                if expected_normalized in normalized:
                    ratio = max(ratio, 0.9)
                for token in expected_normalized.split():
                    if len(token) >= 3 and token in normalized:
                        ratio = max(ratio, 0.9)
                tenant_score = max(tenant_score, ratio)
        if tenant_score >= settings.payment_match_threshold:
            scores[tenant.id] = tenant_score, tenant
    if not scores:
        return None
    highest = max(score for score, _ in scores.values())
    finalists = [
        tenant
        for score, tenant in scores.values()
        if highest - score <= 0.02
    ]
    if len(finalists) == 1:
        return finalists[0]
    amount_tolerance = Decimal(str(settings.payment_amount_tolerance))
    amount_matches = [
        tenant
        for tenant in finalists
        if abs(
            payment.amount
            - (Decimal(tenant.monthly_rent) + Decimal(tenant.maintenance_fee))
        )
        <= amount_tolerance
    ]
    return amount_matches[0] if len(amount_matches) == 1 else None


def reconcile_payments(
    session: Session,
    payments: Iterable[IncomingPayment],
    *,
    dry_run: bool = False,
) -> list[ReconciliationResult]:
    """Match, persist, and apply credits; duplicate bank identifiers are skipped."""
    tenants = list(session.scalars(select(Tenant).where(Tenant.is_active.is_(True))))
    results: list[ReconciliationResult] = []
    for payment in payments:
        if payment.amount <= 0:
            raise ValueError("Incoming payment amount must be positive")
        reference = payment.bank_reference_id or payment.source_transaction_id
        if _is_duplicate(session, payment):
            results.append(ReconciliationResult(reference, None, "DUPLICATE", payment.amount))
            continue
        tenant = match_tenant(payment, tenants)
        if dry_run:
            results.append(
                ReconciliationResult(
                    reference, tenant.id if tenant else None,
                    "MATCHED" if tenant else "UNMATCHED", payment.amount
                )
            )
            continue
        transaction = PaymentTransaction(
            tenant_id=tenant.id if tenant else None,
            bank_reference_id=payment.bank_reference_id,
            source_transaction_id=payment.source_transaction_id,
            amount=payment.amount,
            payment_mode=payment.payment_mode,
            sender_vpa=payment.sender_vpa,
            sender_name=payment.sender_name,
            remarks=payment.remarks,
            timestamp=payment.timestamp,
            is_reconciled=tenant is not None,
        )
        try:
            with session.begin_nested():
                session.add(transaction)
                session.flush()
        except IntegrityError:
            logger.info("Skipping duplicate transaction reference=%s", reference)
            results.append(ReconciliationResult(reference, None, "DUPLICATE", payment.amount))
            continue
        if tenant:
            apply_payment(session, tenant, payment.amount)
            logger.info(
                "Reconciled payment reference=%s tenant_id=%s amount=%s",
                reference, tenant.id, payment.amount
            )
            result_status = "MATCHED"
        else:
            logger.warning("Unmatched incoming payment reference=%s", reference)
            result_status = "UNMATCHED"
        results.append(
            ReconciliationResult(
                reference, tenant.id if tenant else None, result_status, payment.amount
            )
        )
    if not dry_run:
        session.commit()
    return results


def _is_duplicate(session: Session, payment: IncomingPayment) -> bool:
    predicates = []
    if payment.bank_reference_id:
        predicates.append(
            PaymentTransaction.bank_reference_id == payment.bank_reference_id
        )
    if payment.source_transaction_id:
        predicates.append(
            PaymentTransaction.source_transaction_id == payment.source_transaction_id
        )
    if not predicates:
        return False
    return session.scalar(select(PaymentTransaction.id).where(or_(*predicates))) is not None


def apply_payment(session: Session, tenant: Tenant, amount: Decimal) -> None:
    """Apply payment to the latest open ledger, retaining any excess as credit."""
    latest = session.scalar(
        select(MonthlyLedger)
        .where(MonthlyLedger.tenant_id == tenant.id)
        .order_by(MonthlyLedger.billing_month.desc())
        .limit(1)
    )
    if latest is None:
        tenant.credit_balance = Decimal(tenant.credit_balance) + amount
        return
    outstanding = max(Decimal(latest.net_due), Decimal("0.00"))
    applied = min(amount, outstanding)
    latest.amount_paid = Decimal(latest.amount_paid) + applied
    latest.net_due = max(
        Decimal(latest.rent_billed)
        + Decimal(latest.maintenance_billed)
        + Decimal(latest.arrears_brought_forward)
        - Decimal(latest.amount_paid),
        Decimal("0.00"),
    )
    tenant.credit_balance = Decimal(tenant.credit_balance) + (amount - applied)
    latest.status = _ledger_status(latest.amount_paid, latest.net_due)


def _ledger_status(amount_paid: Decimal, net_due: Decimal) -> str:
    if net_due <= 0:
        return "PAID"
    if amount_paid > 0:
        return "PARTIALLY_PAID"
    return "OVERDUE"
