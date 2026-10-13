"""SQLAlchemy models and database session helpers."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from rental_automation.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for application models."""


class LedgerStatus(StrEnum):
    """Monthly account status."""

    PAID = "PAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    OVERDUE = "OVERDUE"


class Tenant(Base):
    """A rental tenant and their current account balance."""

    __tablename__ = "tenants"
    __table_args__ = (UniqueConstraint("unit_number", name="uq_tenant_unit_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    due_day: Mapped[int | None] = mapped_column(Integer)
    upi_vpa: Mapped[str | None] = mapped_column(String(200), index=True)
    monthly_rent: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    maintenance_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    deposit_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    credit_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    identifiers: Mapped[list["TenantIdentifier"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    ledgers: Mapped[list["MonthlyLedger"]] = relationship(back_populates="tenant")


class TenantIdentifier(Base):
    """An expected VPA, bank sender name, or transfer remark."""

    __tablename__ = "tenant_identifiers"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[str] = mapped_column(String(250), nullable=False)
    tenant: Mapped[Tenant] = relationship(back_populates="identifiers")


class PaymentTransaction(Base):
    """An imported bank credit; external identifiers make ingestion idempotent."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    bank_reference_id: Mapped[str | None] = mapped_column(
        String(120), unique=True, index=True
    )
    source_transaction_id: Mapped[str | None] = mapped_column(
        String(160), unique=True, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="UPI")
    sender_vpa: Mapped[str | None] = mapped_column(String(200))
    sender_name: Mapped[str | None] = mapped_column(String(250))
    remarks: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_reconciled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tenant: Mapped[Tenant | None] = relationship()


class MonthlyLedger(Base):
    """A monthly balance snapshot; only the latest ledger is carried forward."""

    __tablename__ = "monthly_ledgers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "billing_month", name="uq_ledger_tenant_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    billing_month: Mapped[date] = mapped_column(Date, nullable=False)
    rent_billed: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    maintenance_billed: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    arrears_brought_forward: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    net_due: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tenant: Mapped[Tenant] = relationship(back_populates="ledgers")


class KSEBMonthlyBill(Base):
    """Combined monthly KSEB readings and payable amount across services."""

    __tablename__ = "kseb_monthly_bills"
    __table_args__ = (
        UniqueConstraint("billing_month", name="uq_kseb_bill_month"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    billing_month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    billing_date: Mapped[date | None] = mapped_column(Date)
    imported_normal_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    imported_off_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    imported_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    exported_normal_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    exported_off_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    exported_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    solar_normal_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    solar_off_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    solar_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    bill_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    source_label: Mapped[str] = mapped_column(String(50), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KSEBConsumerMonthlyBill(Base):
    """Monthly readings and bill amount for one KSEB consumer service."""

    __tablename__ = "kseb_consumer_monthly_bills"
    __table_args__ = (
        UniqueConstraint(
            "billing_month", "consumer_number", name="uq_kseb_consumer_bill_month"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumer_number: Mapped[str] = mapped_column(String(13), nullable=False, index=True)
    billing_month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    billing_date: Mapped[date | None] = mapped_column(Date)
    imported_normal_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    imported_off_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    imported_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    exported_normal_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    exported_off_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    exported_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    solar_normal_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    solar_off_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    solar_peak_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    bill_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    source_label: Mapped[str] = mapped_column(String(50), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RentalAgreement(Base):
    """Immutable agreement terms plus each party's electronic signing record."""

    __tablename__ = "rental_agreements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agreement_code: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    tenant_name: Mapped[str] = mapped_column(String(200), nullable=False)
    tenant_phone: Mapped[str] = mapped_column(String(30), nullable=False)
    tenant_email: Mapped[str | None] = mapped_column(String(320))
    landlord_name: Mapped[str] = mapped_column(String(200), nullable=False)
    landlord_phone: Mapped[str] = mapped_column(String(30), nullable=False)
    landlord_email: Mapped[str | None] = mapped_column(String(320))
    property_address: Mapped[str] = mapped_column(Text, nullable=False)
    check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    rental_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    rental_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    monthly_rent: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    security_deposit: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    rent_due_day: Mapped[int] = mapped_column(Integer, nullable=False)
    additional_terms: Mapped[str] = mapped_column(Text, nullable=False, default="")
    terms_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_sign_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    landlord_sign_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    tenant_signed_name: Mapped[str | None] = mapped_column(String(200))
    landlord_signed_name: Mapped[str | None] = mapped_column(String(200))
    tenant_signature_png: Mapped[bytes | None] = mapped_column(LargeBinary)
    landlord_signature_png: Mapped[bytes | None] = mapped_column(LargeBinary)
    tenant_signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    landlord_signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tenant: Mapped[Tenant] = relationship()
    occupants: Mapped[list["AgreementOccupant"]] = relationship(
        back_populates="agreement", cascade="all, delete-orphan", order_by=lambda: AgreementOccupant.id
    )
    identity_documents: Mapped[list["AgreementIdentityDocument"]] = relationship(
        back_populates="agreement", cascade="all, delete-orphan",
        order_by=lambda: AgreementIdentityDocument.id,
    )


class AgreementOccupant(Base):
    """Additional named residents included in a rental agreement."""

    __tablename__ = "agreement_occupants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agreement_id: Mapped[int] = mapped_column(
        ForeignKey("rental_agreements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    relationship_to_tenant: Mapped[str | None] = mapped_column(String(100))
    agreement: Mapped[RentalAgreement] = relationship(back_populates="occupants")


class AgreementIdentityDocument(Base):
    """An encrypted government ID proof uploaded separately for one occupant."""

    __tablename__ = "agreement_identity_documents"
    __table_args__ = (
        UniqueConstraint("agreement_id", "occupant_key", name="uq_agreement_occupant_id_proof"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agreement_id: Mapped[int] = mapped_column(
        ForeignKey("rental_agreements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    occupant_key: Mapped[str] = mapped_column(String(24), nullable=False)
    occupant_name: Mapped[str] = mapped_column(String(200), nullable=False)
    uploader_name: Mapped[str] = mapped_column(String(200), nullable=False)
    consent_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agreement: Mapped[RentalAgreement] = relationship(back_populates="identity_documents")


class TenantPortalInvite(Base):
    """One revocable private onboarding link for a tenant and their unit."""

    __tablename__ = "tenant_portal_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tenant: Mapped[Tenant] = relationship()


class TenantProfileSubmission(Base):
    """Tenant-provided profile pending manager review."""

    __tablename__ = "tenant_profile_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    upi_vpa: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tenant: Mapped[Tenant] = relationship()
    occupants: Mapped[list["TenantProfileOccupant"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan",
        order_by=lambda: TenantProfileOccupant.id,
    )
    identity_documents: Mapped[list["TenantPortalIdentityDocument"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan",
        order_by=lambda: TenantPortalIdentityDocument.id,
    )


class TenantProfileOccupant(Base):
    """Contact details for one additional resident on a submitted profile."""

    __tablename__ = "tenant_profile_occupants"
    __table_args__ = (UniqueConstraint("submission_id", "occupant_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("tenant_profile_submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    occupant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    relationship_to_tenant: Mapped[str | None] = mapped_column(String(100))
    submission: Mapped[TenantProfileSubmission] = relationship(back_populates="occupants")


class TenantPortalIdentityDocument(Base):
    """Encrypted ID proof supplied separately for a tenant or occupant."""

    __tablename__ = "tenant_portal_identity_documents"
    __table_args__ = (
        UniqueConstraint("submission_id", "occupant_key", name="uq_portal_occupant_id_proof"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("tenant_profile_submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    occupant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    occupant_name: Mapped[str] = mapped_column(String(200), nullable=False)
    uploader_name: Mapped[str] = mapped_column(String(200), nullable=False)
    consent_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submission: Mapped[TenantProfileSubmission] = relationship(back_populates="identity_documents")


def create_database(url: str | None = None) -> tuple[Engine, sessionmaker[Session]]:
    """Create an engine and session factory for the configured database."""
    database_url = url or get_settings().database_url
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def initialize_database(engine: Engine) -> None:
    """Create missing application tables."""
    Base.metadata.create_all(engine)
    # create_all does not add columns to an existing table; upgrade prior local DBs.
    with engine.begin() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("tenant_profile_submissions")
        }
        if "upi_vpa" not in columns:
            connection.execute(
                text("ALTER TABLE tenant_profile_submissions ADD COLUMN upi_vpa VARCHAR(200)")
            )
