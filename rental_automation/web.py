"""Local browser dashboard and JSON API for rental automation."""

import base64
import binascii
import hmac
import ipaddress
import logging
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rental_automation.agreements import (
    agreement_terms_hash,
    build_agreement_pdf,
    create_signing_token,
    hash_signing_token,
)
from rental_automation.ledger import generate_monthly_ledgers, month_start
from rental_automation.config import get_settings
from rental_automation.kseb import (
    fetch_latest_kseb_bill_pdfs,
    parse_kseb_bill_pdf,
    save_kseb_bill,
)
from rental_automation.models import (
    AgreementIdentityDocument,
    AgreementOccupant,
    KSEBConsumerMonthlyBill,
    KSEBMonthlyBill,
    MonthlyLedger,
    PaymentTransaction,
    RentalAgreement,
    Tenant,
    TenantPortalIdentityDocument,
    TenantPortalInvite,
    TenantProfileOccupant,
    TenantProfileSubmission,
    create_database,
    initialize_database,
)
from rental_automation.notification_service import send_monthly_reminders
from rental_automation.reconciliation import IncomingPayment, reconcile_payments
from rental_automation.tenant_registry import (
    TENANT_WORKBOOK_PATH,
    import_tenants_workbook,
    upsert_tenant_workbook_row,
)
from rental_automation.id_documents import decrypt_id_proof, encrypt_id_proof
from rental_automation.solar_workbook import export_solar_workbook, SOLAR_WORKBOOK_PATH

logger = logging.getLogger(__name__)
engine, SessionFactory = create_database()
initialize_database(engine)

app = FastAPI(title="Rental Payment Desk", version="1.0.0")
dashboard_file = Path(__file__).with_name("dashboard.html")
signing_page_file = Path(__file__).with_name("agreement_sign.html")
tenant_portal_file = Path(__file__).with_name("tenant_portal.html")


@app.middleware("http")
async def protect_dashboard_and_signing(request: Request, call_next):
    """Protect manager pages when configured for public HTTPS deployment."""
    settings = get_settings()
    path = request.url.path
    public_signer_route = (
        path in {"/sign", "/api/signing", "/api/signing/pdf", "/api/signing/identity-documents"}
        or re.fullmatch(r"/api/signing/identity-documents/(?:tenant|[0-9]{1,12})", path)
    )
    public_tenant_portal_route = (
        path in {"/tenant", "/api/tenant-portal", "/api/tenant-portal/profile"}
        or re.fullmatch(
            r"/api/tenant-portal/profile/[0-9]{1,12}/identity-documents/[A-Za-z0-9_-]{1,64}",
            path,
        )
    )
    public_capability_route = public_signer_route or public_tenant_portal_route
    client_host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = client_host == "testclient"
    if not settings.public_app_url and not is_loopback and path != "/api/health":
        return Response(
            "Remote access is disabled. Configure HTTPS and manager credentials first.",
            status_code=403,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    if settings.public_app_url and not public_capability_route and path != "/api/health":
        authorization = request.headers.get("authorization", "")
        valid = False
        if authorization.startswith("Basic "):
            try:
                raw_credentials = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                username, separator, password = raw_credentials.partition(":")
                valid = bool(separator) and hmac.compare_digest(
                    username, settings.app_auth_username
                ) and hmac.compare_digest(password, settings.app_auth_password)
            except (binascii.Error, UnicodeDecodeError):
                valid = False
        if not valid:
            return Response(
                "Manager sign-in required.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Rental Payment Desk"'},
            )
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if public_capability_route or path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class TenantCreate(BaseModel):
    """Fields required to create a tenant from the dashboard."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    unit_number: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=1, max_length=30)
    email: str | None = Field(default=None, max_length=320)
    upi_vpa: str | None = Field(default=None, max_length=200)
    monthly_rent: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    maintenance_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    deposit_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    opening_balance: Decimal = Field(default=Decimal("0"), max_digits=12, decimal_places=2)
    due_day: int | None = Field(default=None, ge=1, le=31)
    expected_sender_name: str | None = Field(default=None, max_length=250)
    expected_remarks: str | None = Field(default=None, max_length=250)


class PaymentCreate(BaseModel):
    """Incoming payment fields accepted by the local demo API."""

    model_config = ConfigDict(str_strip_whitespace=True)

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    timestamp: datetime = Field(default_factory=datetime.now)
    bank_reference_id: str = Field(min_length=1, max_length=120)
    payment_mode: str = Field(default="UPI", max_length=20)
    sender_vpa: str | None = Field(default=None, max_length=200)
    sender_name: str | None = Field(default=None, max_length=250)
    remarks: str | None = None


class AgreementOccupantInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=30)
    email: str | None = Field(default=None, max_length=320)
    relationship_to_tenant: str | None = Field(default=None, max_length=100)


class RentalAgreementCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    tenant_id: int = Field(ge=1)
    landlord_name: str = Field(min_length=1, max_length=200)
    landlord_phone: str = Field(min_length=1, max_length=30)
    landlord_email: str | None = Field(default=None, max_length=320)
    property_address: str = Field(min_length=1, max_length=1200)
    check_in_date: date
    rental_start_date: date
    rental_end_date: date
    monthly_rent: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    security_deposit: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    rent_due_day: int = Field(default=5, ge=1, le=31)
    additional_terms: str = Field(default="", max_length=10000)
    occupants: list[AgreementOccupantInput] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_dates(self) -> "RentalAgreementCreate":
        if self.rental_end_date < self.rental_start_date:
            raise ValueError("Rental end date must be on or after the start date.")
        if self.check_in_date > self.rental_end_date:
            raise ValueError("Check-in date must be on or before the rental end date.")
        return self


class AgreementSignatureInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    signer_name: str = Field(min_length=1, max_length=200)
    signature_data_url: str = Field(min_length=1, max_length=180000)
    consent: bool


class TenantProfileOccupantInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    occupant_key: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=30)
    email: str | None = Field(default=None, max_length=320)
    relationship_to_tenant: str | None = Field(default=None, max_length=100)


class TenantProfileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=30)
    email: str = Field(min_length=1, max_length=320)
    upi_vpa: str | None = Field(default=None, max_length=200)
    occupants: list[TenantProfileOccupantInput] = Field(default_factory=list, max_length=12)


def _tenant_dict(tenant: Tenant) -> dict:
    return {
        "id": tenant.id,
        "name": tenant.name,
        "unit_number": tenant.unit_number,
        "phone": tenant.phone,
        "email": tenant.email,
        "upi_vpa": tenant.upi_vpa,
        "monthly_rent": str(tenant.monthly_rent),
        "maintenance_fee": str(tenant.maintenance_fee),
        "deposit_amount": str(tenant.deposit_amount),
        "opening_balance": str(tenant.opening_balance),
        "credit_balance": str(tenant.credit_balance),
        "due_day": tenant.due_day,
        "expected_sender_name": next(
            (item.value for item in tenant.identifiers if item.kind == "sender_name"), None
        ),
        "expected_remarks": next(
            (item.value for item in tenant.identifiers if item.kind == "remarks"), None
        ),
        "is_active": tenant.is_active,
    }


def _sync_tenant_workbook(session: Session) -> None:
    """Keep the database used by billing and matching aligned with the workbook."""
    try:
        import_tenants_workbook(session, TENANT_WORKBOOK_PATH)
    except (OSError, ValueError) as exc:
        logger.exception("Unable to sync tenant workbook")
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not read {TENANT_WORKBOOK_PATH.name}. Check its Tenants sheet "
                "and required columns, save the workbook, then refresh."
            ),
        ) from exc


def _ledger_dict(ledger: MonthlyLedger, tenant: Tenant) -> dict:
    return {
        "tenant_id": tenant.id,
        "tenant_name": tenant.name,
        "unit_number": tenant.unit_number,
        "billing_month": ledger.billing_month.strftime("%Y-%m"),
        "rent_billed": str(ledger.rent_billed),
        "maintenance_billed": str(ledger.maintenance_billed),
        "arrears_brought_forward": str(ledger.arrears_brought_forward),
        "amount_paid": str(ledger.amount_paid),
        "net_due": str(ledger.net_due),
        "status": ledger.status,
        "reminder_sent_at": (
            ledger.reminder_sent_at.isoformat() if ledger.reminder_sent_at else None
        ),
    }


def _kseb_bill_dict(bill: KSEBMonthlyBill) -> dict:
    """Return readings and derived usage for one service or the monthly total."""
    imported = sum(
        (
            bill.imported_normal_kwh,
            bill.imported_off_peak_kwh,
            bill.imported_peak_kwh,
        ),
        Decimal("0"),
    )
    exported = sum(
        (
            bill.exported_normal_kwh,
            bill.exported_off_peak_kwh,
            bill.exported_peak_kwh,
        ),
        Decimal("0"),
    )
    generation = sum(
        (bill.solar_normal_kwh, bill.solar_off_peak_kwh, bill.solar_peak_kwh),
        Decimal("0"),
    )
    return {
        "billing_month": bill.billing_month.strftime("%Y-%m"),
        "billing_date": bill.billing_date.isoformat() if bill.billing_date else None,
        "imported_normal_kwh": str(bill.imported_normal_kwh),
        "imported_off_peak_kwh": str(bill.imported_off_peak_kwh),
        "imported_peak_kwh": str(bill.imported_peak_kwh),
        "imported_total_kwh": str(imported),
        "exported_normal_kwh": str(bill.exported_normal_kwh),
        "exported_off_peak_kwh": str(bill.exported_off_peak_kwh),
        "exported_peak_kwh": str(bill.exported_peak_kwh),
        "exported_total_kwh": str(exported),
        "solar_normal_kwh": str(bill.solar_normal_kwh),
        "solar_off_peak_kwh": str(bill.solar_off_peak_kwh),
        "solar_peak_kwh": str(bill.solar_peak_kwh),
        "solar_generation_kwh": str(generation),
        "solar_self_used_kwh": str(generation - exported),
        "site_consumption_kwh": str(generation + imported - exported),
        "bill_amount": str(bill.bill_amount),
        "source_label": bill.source_label,
        "fetched_at": bill.fetched_at.isoformat(),
    }


def _kseb_consumer_bill_dict(bill: KSEBConsumerMonthlyBill) -> dict:
    """Return per-service readings for the authenticated property manager UI."""
    return {**_kseb_bill_dict(bill), "consumer_number": bill.consumer_number}


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serve the local interactive dashboard."""
    return FileResponse(dashboard_file, media_type="text/html")


@app.get("/sign", response_class=HTMLResponse, include_in_schema=False)
def agreement_signing_page() -> HTMLResponse:
    """Serve the public signing form; the token stays in the browser URL fragment."""
    return HTMLResponse(signing_page_file.read_text(encoding="utf-8"))


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _agreement_dict(agreement: RentalAgreement) -> dict:
    return {
        "id": agreement.id,
        "agreement_code": agreement.agreement_code,
        "tenant_name": agreement.tenant_name,
        "tenant_phone": agreement.tenant_phone,
        "unit_number": agreement.tenant.unit_number if agreement.tenant else "",
        "landlord_name": agreement.landlord_name,
        "check_in_date": agreement.check_in_date.isoformat(),
        "rental_start_date": agreement.rental_start_date.isoformat(),
        "rental_end_date": agreement.rental_end_date.isoformat(),
        "monthly_rent": str(agreement.monthly_rent),
        "security_deposit": str(agreement.security_deposit),
        "status": agreement.status,
        "occupant_count": len(agreement.occupants),
        "identity_document_count": len(agreement.identity_documents),
        "created_at": _utc_iso(agreement.created_at),
        "signed": agreement.status == "SIGNED",
        "tenant_signed": agreement.tenant_signed_at is not None,
        "landlord_signed": agreement.landlord_signed_at is not None,
    }


def _lookup_signing_link(session: Session, token: str) -> tuple[RentalAgreement, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
        raise HTTPException(status_code=404, detail="Signing link not found.")
    token_hash = hash_signing_token(token)
    agreement = session.scalar(
        select(RentalAgreement).where(
            or_(
                RentalAgreement.tenant_sign_token_hash == token_hash,
                RentalAgreement.landlord_sign_token_hash == token_hash,
            )
        )
    )
    if agreement is None:
        raise HTTPException(status_code=404, detail="Signing link not found.")
    role = "tenant" if hmac.compare_digest(agreement.tenant_sign_token_hash, token_hash) else "landlord"
    return agreement, role


def _signing_token_from_request(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
        raise HTTPException(status_code=404, detail="Signing link not found.")
    return token


def _check_agreement_terms(agreement: RentalAgreement) -> None:
    if not hmac.compare_digest(
        agreement.terms_sha256,
        agreement_terms_hash(agreement, agreement.occupants),
    ):
        raise HTTPException(
            status_code=409,
            detail="The saved agreement terms changed. Do not sign; create a new agreement copy.",
        )


def _signer_payload(agreement: RentalAgreement, role: str) -> dict:
    signer_name = agreement.tenant_name if role == "tenant" else agreement.landlord_name
    signed_name = agreement.tenant_signed_name if role == "tenant" else agreement.landlord_signed_name
    signed_at = agreement.tenant_signed_at if role == "tenant" else agreement.landlord_signed_at
    return {
        "agreement_code": agreement.agreement_code,
        "role": role,
        "expected_signer_name": signer_name,
        "signed_by_me": signed_at is not None,
        "signed_name": signed_name,
        "signed_at": _utc_iso(signed_at),
        "status": agreement.status,
        "pdf_available": agreement.status == "SIGNED",
        "tenant": {
            "name": agreement.tenant_name,
            "phone": agreement.tenant_phone,
            "email": agreement.tenant_email,
        },
        "landlord": {
            "name": agreement.landlord_name,
            "phone": agreement.landlord_phone,
            "email": agreement.landlord_email,
        },
        "property_address": agreement.property_address,
        "check_in_date": agreement.check_in_date.isoformat(),
        "rental_start_date": agreement.rental_start_date.isoformat(),
        "rental_end_date": agreement.rental_end_date.isoformat(),
        "monthly_rent": str(agreement.monthly_rent),
        "security_deposit": str(agreement.security_deposit),
        "rent_due_day": agreement.rent_due_day,
        "additional_terms": agreement.additional_terms,
        "occupants": [
            {
                "name": item.name,
                "phone": item.phone,
                "email": item.email,
                "relationship_to_tenant": item.relationship_to_tenant,
            }
            for item in agreement.occupants
        ],
    }


def _identity_occupants(agreement: RentalAgreement) -> list[tuple[str, str]]:
    return [("tenant", agreement.tenant_name)] + [
        (str(occupant.id), occupant.name) for occupant in agreement.occupants
    ]


def _identity_document_status(agreement: RentalAgreement) -> list[dict]:
    stored = {item.occupant_key: item for item in agreement.identity_documents}
    result = []
    for key, name in _identity_occupants(agreement):
        document = stored.get(key)
        result.append({
            "occupant_key": key,
            "occupant_name": name,
            "uploaded": document is not None,
            "content_type": document.content_type if document else None,
            "byte_length": document.byte_length if document else None,
            "uploaded_at": _utc_iso(document.uploaded_at) if document else None,
        })
    return result


def _identity_occupant_name(agreement: RentalAgreement, occupant_key: str) -> str:
    if occupant_key == "tenant":
        return agreement.tenant_name
    if not re.fullmatch(r"[0-9]{1,12}", occupant_key):
        raise HTTPException(status_code=404, detail="Occupant not found in this agreement.")
    occupant_id = int(occupant_key)
    occupant = next((item for item in agreement.occupants if item.id == occupant_id), None)
    if occupant is None:
        raise HTTPException(status_code=404, detail="Occupant not found in this agreement.")
    return occupant.name


def _document_content_type(content_type: str, contents: bytes) -> str:
    valid = {
        "application/pdf": contents.startswith(b"%PDF-"),
        "image/jpeg": contents.startswith(b"\xff\xd8\xff"),
        "image/png": contents.startswith(b"\x89PNG\r\n\x1a\n"),
    }
    if content_type not in valid or not valid[content_type]:
        raise HTTPException(
            status_code=415,
            detail="Upload a PDF, JPEG, or PNG ID proof. The file type must match its contents.",
        )
    return content_type


@app.get("/api/signing/identity-documents")
def list_signer_identity_documents(request: Request) -> dict:
    """List each occupant's upload status for the main tenant's signing link."""
    token = _signing_token_from_request(request)
    with SessionFactory() as session:
        agreement, role = _lookup_signing_link(session, token)
        if role != "tenant":
            raise HTTPException(status_code=403, detail="Only the main tenant link can manage occupant ID proofs.")
        return {"occupants": _identity_document_status(agreement)}


@app.post("/api/signing/identity-documents/{occupant_key}")
async def upload_signer_identity_document(occupant_key: str, request: Request) -> dict:
    """Encrypt and save one occupant's PDF/JPEG/PNG proof from the main tenant link."""
    token = _signing_token_from_request(request)
    if request.headers.get("x-id-proof-consent", "").casefold() != "true":
        raise HTTPException(
            status_code=422,
            detail="Confirm that you are authorized to submit this occupant's ID proof.",
        )
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/pdf", "image/jpeg", "image/png"}:
        raise HTTPException(status_code=415, detail="Upload a PDF, JPEG, or PNG ID proof.")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Each ID proof must be 8 MB or smaller.")

    with SessionFactory() as session:
        agreement, role = _lookup_signing_link(session, token)
        if role != "tenant":
            raise HTTPException(status_code=403, detail="Only the main tenant link can manage occupant ID proofs.")
        _check_agreement_terms(agreement)
        occupant_name = _identity_occupant_name(agreement, occupant_key)
        chunks = []
        byte_length = 0
        async for chunk in request.stream():
            byte_length += len(chunk)
            if byte_length > 8 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Each ID proof must be 8 MB or smaller.")
            chunks.append(chunk)
        contents = b"".join(chunks)
        if not contents:
            raise HTTPException(status_code=422, detail="The selected ID proof is empty.")
        _document_content_type(content_type, contents)
        try:
            encrypted, digest = encrypt_id_proof(contents)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        uploaded_at = datetime.now(timezone.utc)
        document = session.scalar(
            select(AgreementIdentityDocument).where(
                AgreementIdentityDocument.agreement_id == agreement.id,
                AgreementIdentityDocument.occupant_key == occupant_key,
            )
        )
        if document is None:
            document = AgreementIdentityDocument(
                agreement_id=agreement.id,
                occupant_key=occupant_key,
                occupant_name=occupant_name,
                uploader_name=agreement.tenant_name,
                consent_confirmed_at=uploaded_at,
                content_type=content_type,
                byte_length=byte_length,
                content_sha256=digest,
                encrypted_content=encrypted,
                uploaded_at=uploaded_at,
            )
            session.add(document)
        else:
            document.occupant_name = occupant_name
            document.uploader_name = agreement.tenant_name
            document.consent_confirmed_at = uploaded_at
            document.content_type = content_type
            document.byte_length = byte_length
            document.content_sha256 = digest
            document.encrypted_content = encrypted
            document.uploaded_at = uploaded_at
        session.commit()
        return {
            "occupant_key": occupant_key,
            "occupant_name": occupant_name,
            "uploaded": True,
            "byte_length": byte_length,
            "content_type": content_type,
            "uploaded_at": _utc_iso(document.uploaded_at),
        }


@app.get("/api/agreements/{agreement_id}/identity-documents")
def list_manager_identity_documents(agreement_id: int) -> dict:
    """List occupant-level proof status for an agreement's unit in the manager UI."""
    with SessionFactory() as session:
        agreement = session.get(RentalAgreement, agreement_id)
        if agreement is None:
            raise HTTPException(status_code=404, detail="Agreement not found.")
        return {
            "agreement_id": agreement.id,
            "unit_number": agreement.tenant.unit_number if agreement.tenant else "",
            "tenant_name": agreement.tenant_name,
            "occupants": _identity_document_status(agreement),
        }


@app.get("/api/agreements/{agreement_id}/identity-documents/{occupant_key}")
def download_manager_identity_document(agreement_id: int, occupant_key: str) -> Response:
    """Download one decrypted ID proof from the protected manager dashboard."""
    with SessionFactory() as session:
        agreement = session.get(RentalAgreement, agreement_id)
        if agreement is None:
            raise HTTPException(status_code=404, detail="Agreement not found.")
        occupant_name = _identity_occupant_name(agreement, occupant_key)
        document = session.scalar(
            select(AgreementIdentityDocument).where(
                AgreementIdentityDocument.agreement_id == agreement.id,
                AgreementIdentityDocument.occupant_key == occupant_key,
            )
        )
        if document is None:
            raise HTTPException(status_code=404, detail="No ID proof has been uploaded for this occupant.")
        try:
            content = decrypt_id_proof(document.encrypted_content, document.content_sha256)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        suffix = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[document.content_type]
        return Response(
            content=content,
            media_type=document.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="id-proof-{agreement.agreement_code}-{occupant_key}.{suffix}"',
                "Cache-Control": "no-store",
            },
        )


@app.get("/api/agreements")
def list_agreements() -> dict:
    """List manager-facing agreement status without returning signing tokens."""
    with SessionFactory() as session:
        agreements = list(
            session.scalars(
                select(RentalAgreement).order_by(RentalAgreement.created_at.desc())
            )
        )
        return {"agreements": [_agreement_dict(item) for item in agreements]}


@app.post("/api/agreements", status_code=201)
def create_agreement(payload: RentalAgreementCreate, request: Request) -> dict:
    """Snapshot the tenant's details and create independent tenant/landlord links."""
    tenant_token = create_signing_token()
    landlord_token = create_signing_token()
    with SessionFactory() as session:
        _sync_tenant_workbook(session)
        tenant = session.get(Tenant, payload.tenant_id)
        if tenant is None or not tenant.is_active:
            raise HTTPException(status_code=422, detail="Choose an active tenant from the registry.")
        agreement = RentalAgreement(
            agreement_code=str(uuid.uuid4()),
            tenant_id=tenant.id,
            tenant_name=tenant.name,
            tenant_phone=tenant.phone,
            tenant_email=tenant.email,
            landlord_name=payload.landlord_name,
            landlord_phone=payload.landlord_phone,
            landlord_email=payload.landlord_email or None,
            property_address=payload.property_address,
            check_in_date=payload.check_in_date,
            rental_start_date=payload.rental_start_date,
            rental_end_date=payload.rental_end_date,
            monthly_rent=payload.monthly_rent,
            security_deposit=payload.security_deposit,
            rent_due_day=payload.rent_due_day,
            additional_terms=payload.additional_terms,
            terms_sha256="",
            tenant_sign_token_hash=hash_signing_token(tenant_token),
            landlord_sign_token_hash=hash_signing_token(landlord_token),
            status="PENDING",
            created_at=datetime.now(timezone.utc),
            occupants=[
                AgreementOccupant(
                    name=item.name,
                    phone=item.phone,
                    email=item.email or None,
                    relationship_to_tenant=item.relationship_to_tenant or None,
                )
                for item in payload.occupants
            ],
        )
        session.add(agreement)
        session.flush()
        agreement.terms_sha256 = agreement_terms_hash(agreement, agreement.occupants)
        session.commit()
        summary = _agreement_dict(agreement)

    settings = get_settings()
    base_url = settings.public_app_url.rstrip("/") if settings.public_app_url else str(request.base_url).rstrip("/")
    return {
        "agreement": summary,
        "signing_links": {
            "tenant": f"{base_url}/sign#{tenant_token}",
            "landlord": f"{base_url}/sign#{landlord_token}",
        },
        "remote_signing_configured": bool(settings.public_app_url),
    }


@app.post("/api/agreements/{agreement_id}/signing-link/{role}")
def replace_agreement_signing_link(agreement_id: int, role: str, request: Request) -> dict:
    """Issue a fresh link for an unsigned party; the previous link stops working."""
    if role not in {"tenant", "landlord"}:
        raise HTTPException(status_code=422, detail="Role must be tenant or landlord.")
    token = create_signing_token()
    with SessionFactory() as session:
        agreement = session.get(RentalAgreement, agreement_id)
        if agreement is None:
            raise HTTPException(status_code=404, detail="Agreement not found.")
        if getattr(agreement, f"{role}_signed_at") is not None:
            raise HTTPException(status_code=409, detail="This party has already signed.")
        setattr(agreement, f"{role}_sign_token_hash", hash_signing_token(token))
        session.commit()
    settings = get_settings()
    base_url = settings.public_app_url.rstrip("/") if settings.public_app_url else str(request.base_url).rstrip("/")
    return {
        "role": role,
        "signing_url": f"{base_url}/sign#{token}",
        "remote_signing_configured": bool(settings.public_app_url),
    }


@app.get("/api/signing")
def get_signing_agreement(request: Request) -> dict:
    """Return only the agreement addressed by this party's bearer link."""
    token = _signing_token_from_request(request)
    with SessionFactory() as session:
        agreement, role = _lookup_signing_link(session, token)
        _check_agreement_terms(agreement)
        return _signer_payload(agreement, role)


@app.post("/api/signing")
def sign_agreement(request: Request, payload: AgreementSignatureInput) -> dict:
    """Record one party's consent, typed name, signature image, and server timestamp."""
    token = _signing_token_from_request(request)
    if not payload.consent:
        raise HTTPException(status_code=422, detail="Confirm that you agree to the displayed terms.")
    match = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", payload.signature_data_url)
    if not match:
        raise HTTPException(status_code=422, detail="Provide a drawn PNG signature.")
    try:
        signature_png = base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="The signature image could not be read.") from exc
    if len(signature_png) > 128 * 1024 or not signature_png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=422, detail="Signature image must be a PNG under 128 KB.")

    with SessionFactory() as session:
        agreement, role = _lookup_signing_link(session, token)
        _check_agreement_terms(agreement)
        name_field = f"{role}_signed_name"
        image_field = f"{role}_signature_png"
        timestamp_field = f"{role}_signed_at"
        if getattr(agreement, timestamp_field) is not None:
            raise HTTPException(status_code=409, detail="This party has already signed the agreement.")
        expected_name = agreement.tenant_name if role == "tenant" else agreement.landlord_name
        if " ".join(payload.signer_name.casefold().split()) != " ".join(expected_name.casefold().split()):
            raise HTTPException(
                status_code=422,
                detail=f"Enter the agreement name exactly as shown: {expected_name}.",
            )
        setattr(agreement, name_field, payload.signer_name)
        setattr(agreement, image_field, signature_png)
        setattr(agreement, timestamp_field, datetime.now(timezone.utc))
        agreement.status = (
            "SIGNED"
            if agreement.tenant_signed_at is not None and agreement.landlord_signed_at is not None
            else "PARTIALLY_SIGNED"
        )
        session.commit()
        return {
            "status": agreement.status,
            "signed_at": _utc_iso(getattr(agreement, timestamp_field)),
            "pdf_available": agreement.status == "SIGNED",
        }


def _agreement_pdf_response(agreement: RentalAgreement) -> Response:
    if agreement.status != "SIGNED":
        raise HTTPException(status_code=409, detail="The PDF is available after both parties sign.")
    _check_agreement_terms(agreement)
    try:
        pdf = build_agreement_pdf(agreement)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="rental-agreement-{agreement.agreement_code}.pdf"'},
    )


@app.get("/api/agreements/{agreement_id}/pdf")
def download_agreement_pdf(agreement_id: int) -> Response:
    """Download a fully signed agreement from the protected manager dashboard."""
    with SessionFactory() as session:
        agreement = session.get(RentalAgreement, agreement_id)
        if agreement is None:
            raise HTTPException(status_code=404, detail="Agreement not found.")
        return _agreement_pdf_response(agreement)


@app.get("/api/signing/pdf")
def download_signed_agreement_for_party(request: Request) -> Response:
    """Let either signatory retrieve the completed PDF through their own link."""
    token = _signing_token_from_request(request)
    with SessionFactory() as session:
        agreement, _ = _lookup_signing_link(session, token)
        return _agreement_pdf_response(agreement)


def _tenant_portal_access(request: Request, session: Session) -> tuple[TenantPortalInvite, Tenant]:
    """Resolve a tenant's bearer link without placing its secret in request URLs."""
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
        raise HTTPException(status_code=404, detail="Tenant onboarding link not found.")
    token_hash = hash_signing_token(token)
    invite = session.scalar(
        select(TenantPortalInvite).where(TenantPortalInvite.token_hash == token_hash)
    )
    if invite is None or invite.tenant is None or not invite.tenant.is_active:
        raise HTTPException(status_code=404, detail="Tenant onboarding link not found.")
    return invite, invite.tenant


def _latest_tenant_submission(session: Session, tenant_id: int) -> TenantProfileSubmission | None:
    return session.scalar(
        select(TenantProfileSubmission)
        .where(TenantProfileSubmission.tenant_id == tenant_id)
        .order_by(TenantProfileSubmission.submitted_at.desc(), TenantProfileSubmission.id.desc())
        .limit(1)
    )


def _portal_document_status(submission: TenantProfileSubmission) -> list[dict]:
    documents = {item.occupant_key: item for item in submission.identity_documents}
    occupants = [("tenant", submission.name)] + [
        (item.occupant_key, item.name) for item in submission.occupants
    ]
    return [
        {
            "occupant_key": key,
            "occupant_name": name,
            "uploaded": key in documents,
            "content_type": documents[key].content_type if key in documents else None,
            "byte_length": documents[key].byte_length if key in documents else None,
            "uploaded_at": _utc_iso(documents[key].uploaded_at) if key in documents else None,
        }
        for key, name in occupants
    ]


def _portal_submission_dict(submission: TenantProfileSubmission) -> dict:
    tenant = submission.tenant
    return {
        "id": submission.id,
        "tenant_id": submission.tenant_id,
        "tenant_name": submission.name,
        "tenant_phone": submission.phone,
        "tenant_email": submission.email,
        "upi_vpa": submission.upi_vpa,
        "unit_number": tenant.unit_number if tenant else "",
        "status": submission.status,
        "submitted_at": _utc_iso(submission.submitted_at),
        "reviewed_at": _utc_iso(submission.reviewed_at),
        "occupants": [
            {
                "occupant_key": item.occupant_key,
                "name": item.name,
                "phone": item.phone,
                "email": item.email,
                "relationship_to_tenant": item.relationship_to_tenant,
            }
            for item in submission.occupants
        ],
        "identity_documents": _portal_document_status(submission),
    }


@app.get("/tenant", response_class=HTMLResponse, include_in_schema=False)
def tenant_portal_page() -> HTMLResponse:
    """Serve the separate tenant onboarding and identity-proof upload page."""
    return HTMLResponse(tenant_portal_file.read_text(encoding="utf-8"))


@app.get("/api/tenant-portal")
def get_tenant_portal_profile(request: Request) -> dict:
    """Load the unit and latest profile for the tenant's private onboarding link."""
    with SessionFactory() as session:
        _, tenant = _tenant_portal_access(request, session)
        latest = _latest_tenant_submission(session, tenant.id)
        profile = latest
        return {
            "unit_number": tenant.unit_number,
            "tenant": {
                "name": profile.name if profile else tenant.name,
                "phone": profile.phone if profile else tenant.phone,
                "email": profile.email if profile else tenant.email,
                "upi_vpa": (profile.upi_vpa if profile else None) or tenant.upi_vpa,
            },
            "submission": _portal_submission_dict(latest) if latest else None,
        }


@app.post("/api/tenant-portal/profile", status_code=201)
def submit_tenant_portal_profile(request: Request, payload: TenantProfileInput) -> dict:
    """Save contact/occupant details as a reviewable submission for this unit."""
    keys = [item.occupant_key for item in payload.occupants]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="Each additional occupant must have a unique key.")
    if "tenant" in keys:
        raise HTTPException(status_code=422, detail="The main tenant is entered separately.")

    with SessionFactory() as session:
        _, tenant = _tenant_portal_access(request, session)
        submission = _latest_tenant_submission(session, tenant.id)
        now = datetime.now(timezone.utc)
        if submission is None or submission.status != "PENDING":
            submission = TenantProfileSubmission(
                tenant_id=tenant.id,
                name=payload.name,
                phone=payload.phone,
                email=payload.email,
                upi_vpa=payload.upi_vpa or None,
                status="PENDING",
                submitted_at=now,
            )
            session.add(submission)
            session.flush()
        else:
            submission.name = payload.name
            submission.phone = payload.phone
            submission.email = payload.email
            submission.upi_vpa = payload.upi_vpa or None
            submission.submitted_at = now

        existing = {item.occupant_key: item for item in submission.occupants}
        incoming_keys = set(keys)
        for key, occupant in list(existing.items()):
            if key not in incoming_keys:
                session.delete(occupant)
        valid_document_keys = incoming_keys | {"tenant"}
        for document in list(submission.identity_documents):
            if document.occupant_key not in valid_document_keys:
                session.delete(document)

        for item in payload.occupants:
            occupant = existing.get(item.occupant_key)
            if occupant is None:
                occupant = TenantProfileOccupant(
                    submission_id=submission.id,
                    occupant_key=item.occupant_key,
                    name=item.name,
                    phone=item.phone,
                    email=item.email or None,
                    relationship_to_tenant=item.relationship_to_tenant or None,
                )
                session.add(occupant)
            else:
                occupant.name = item.name
                occupant.phone = item.phone
                occupant.email = item.email or None
                occupant.relationship_to_tenant = item.relationship_to_tenant or None
            for document in submission.identity_documents:
                if document.occupant_key == item.occupant_key:
                    document.occupant_name = item.name
        for document in submission.identity_documents:
            if document.occupant_key == "tenant":
                document.occupant_name = payload.name

        session.commit()
        session.refresh(submission)
        return {"submission": _portal_submission_dict(submission)}


@app.post("/api/tenant-portal/profile/{submission_id}/identity-documents/{occupant_key}")
async def upload_tenant_portal_identity_document(
    submission_id: int, occupant_key: str, request: Request
) -> dict:
    """Encrypt and save one PDF/JPEG/PNG proof for the named occupant."""
    if request.headers.get("x-id-proof-consent", "").casefold() != "true":
        raise HTTPException(
            status_code=422,
            detail="Confirm that you are authorized to submit this occupant's ID proof.",
        )
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/pdf", "image/jpeg", "image/png"}:
        raise HTTPException(status_code=415, detail="Upload a PDF, JPEG, or PNG ID proof.")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Each ID proof must be 8 MB or smaller.")

    with SessionFactory() as session:
        _, tenant = _tenant_portal_access(request, session)
        submission = session.get(TenantProfileSubmission, submission_id)
        if submission is None or submission.tenant_id != tenant.id:
            raise HTTPException(status_code=404, detail="Profile submission not found.")
        latest = _latest_tenant_submission(session, tenant.id)
        if latest is None or latest.id != submission.id:
            raise HTTPException(status_code=404, detail="Profile submission not found.")
        if occupant_key == "tenant":
            occupant_name = submission.name
        else:
            occupant = next(
                (item for item in submission.occupants if item.occupant_key == occupant_key), None
            )
            if occupant is None:
                raise HTTPException(status_code=404, detail="Occupant not found in this submission.")
            occupant_name = occupant.name

        chunks = []
        byte_length = 0
        async for chunk in request.stream():
            byte_length += len(chunk)
            if byte_length > 8 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Each ID proof must be 8 MB or smaller.")
            chunks.append(chunk)
        contents = b"".join(chunks)
        if not contents:
            raise HTTPException(status_code=422, detail="The selected ID proof is empty.")
        _document_content_type(content_type, contents)
        try:
            encrypted, digest = encrypt_id_proof(contents)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        now = datetime.now(timezone.utc)
        document = session.scalar(
            select(TenantPortalIdentityDocument).where(
                TenantPortalIdentityDocument.submission_id == submission.id,
                TenantPortalIdentityDocument.occupant_key == occupant_key,
            )
        )
        if document is None:
            document = TenantPortalIdentityDocument(
                submission_id=submission.id,
                occupant_key=occupant_key,
                occupant_name=occupant_name,
                uploader_name=submission.name,
                consent_confirmed_at=now,
                content_type=content_type,
                byte_length=byte_length,
                content_sha256=digest,
                encrypted_content=encrypted,
                uploaded_at=now,
            )
            session.add(document)
        else:
            document.occupant_name = occupant_name
            document.uploader_name = submission.name
            document.consent_confirmed_at = now
            document.content_type = content_type
            document.byte_length = byte_length
            document.content_sha256 = digest
            document.encrypted_content = encrypted
            document.uploaded_at = now
        session.commit()
        return {
            "occupant_key": occupant_key,
            "occupant_name": occupant_name,
            "uploaded": True,
            "byte_length": byte_length,
            "content_type": content_type,
            "uploaded_at": _utc_iso(document.uploaded_at),
        }


@app.post("/api/tenant-portal/invites/{tenant_id}")
def create_tenant_portal_invite(tenant_id: int, request: Request) -> dict:
    """Create or rotate the manager-issued onboarding link for an active unit."""
    token = create_signing_token()
    token_hash = hash_signing_token(token)
    with SessionFactory() as session:
        _sync_tenant_workbook(session)
        tenant = session.get(Tenant, tenant_id)
        if tenant is None or not tenant.is_active:
            raise HTTPException(status_code=404, detail="Active tenant not found.")
        invite = session.scalar(
            select(TenantPortalInvite).where(TenantPortalInvite.tenant_id == tenant.id)
        )
        if invite is None:
            invite = TenantPortalInvite(
                tenant_id=tenant.id,
                token_hash=token_hash,
                created_at=datetime.now(timezone.utc),
            )
            session.add(invite)
        else:
            invite.token_hash = token_hash
            invite.created_at = datetime.now(timezone.utc)
        session.commit()
    settings = get_settings()
    base_url = settings.public_app_url.rstrip("/") if settings.public_app_url else str(request.base_url).rstrip("/")
    return {
        "unit_number": tenant.unit_number,
        "tenant_name": tenant.name,
        "tenant_phone": tenant.phone,
        "portal_url": f"{base_url}/tenant#{token}",
        "remote_portal_configured": bool(settings.public_app_url),
    }


@app.get("/api/tenant-portal/submissions")
def list_tenant_profile_submissions() -> dict:
    """List submitted profiles and ID-proof availability for manager review."""
    with SessionFactory() as session:
        submissions = list(
            session.scalars(
                select(TenantProfileSubmission)
                .order_by(TenantProfileSubmission.submitted_at.desc())
                .limit(100)
            )
        )
        return {"submissions": [_portal_submission_dict(item) for item in submissions]}


@app.post("/api/tenant-portal/submissions/{submission_id}/approve")
def approve_tenant_profile_submission(submission_id: int) -> dict:
    """Approve submitted contact details and sync them to the tenant workbook."""
    with SessionFactory() as session:
        submission = session.get(TenantProfileSubmission, submission_id)
        if submission is None:
            raise HTTPException(status_code=404, detail="Profile submission not found.")
        if submission.status != "PENDING":
            raise HTTPException(status_code=409, detail="This profile has already been reviewed.")
        tenant = session.get(Tenant, submission.tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="The submitted tenant no longer exists.")
        values = {
            "name": submission.name,
            "unit_number": tenant.unit_number,
            "phone": submission.phone,
            "email": submission.email,
            "upi_vpa": submission.upi_vpa or tenant.upi_vpa,
            "monthly_rent": tenant.monthly_rent,
            "maintenance_fee": tenant.maintenance_fee,
            "deposit_amount": tenant.deposit_amount,
            "opening_balance": tenant.opening_balance,
            "due_day": tenant.due_day,
            "is_active": tenant.is_active,
            "expected_sender_name": next(
                (item.value for item in tenant.identifiers if item.kind == "sender_name"), None
            ),
            "expected_remarks": next(
                (item.value for item in tenant.identifiers if item.kind == "remarks"), None
            ),
        }
        try:
            upsert_tenant_workbook_row(values, TENANT_WORKBOOK_PATH)
        except (OSError, ValueError) as exc:
            logger.exception("Unable to approve tenant profile into workbook")
            raise HTTPException(
                status_code=503,
                detail=f"Could not save {TENANT_WORKBOOK_PATH.name}. Close it in Excel and try again.",
            ) from exc
        tenant.name = submission.name
        tenant.phone = submission.phone
        tenant.email = submission.email
        tenant.upi_vpa = submission.upi_vpa or tenant.upi_vpa
        submission.status = "APPROVED"
        submission.reviewed_at = datetime.now(timezone.utc)
        session.commit()
        return {"submission": _portal_submission_dict(submission), "tenant": _tenant_dict(tenant)}


@app.get("/api/tenant-portal/submissions/{submission_id}/identity-documents/{occupant_key}")
def download_tenant_portal_identity_document(submission_id: int, occupant_key: str) -> Response:
    """Download one decrypted occupant ID proof through the manager-only API."""
    with SessionFactory() as session:
        submission = session.get(TenantProfileSubmission, submission_id)
        if submission is None:
            raise HTTPException(status_code=404, detail="Profile submission not found.")
        document = session.scalar(
            select(TenantPortalIdentityDocument).where(
                TenantPortalIdentityDocument.submission_id == submission.id,
                TenantPortalIdentityDocument.occupant_key == occupant_key,
            )
        )
        if document is None:
            raise HTTPException(status_code=404, detail="No ID proof has been uploaded for this occupant.")
        try:
            content = decrypt_id_proof(document.encrypted_content, document.content_sha256)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        suffix = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[document.content_type]
        return Response(
            content=content,
            media_type=document.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="tenant-id-proof-{submission.tenant_id}-{occupant_key}.{suffix}"',
                "Cache-Control": "no-store",
            },
        )


@app.get("/api/health")
def health() -> dict[str, str]:
    """Simple readiness endpoint."""
    return {"status": "ok"}


@app.get("/api/dashboard")
def dashboard_data(month: str | None = None) -> dict:
    """Return tenant, ledger, and transaction summary for the dashboard."""
    billing_month = _parse_month(month)
    with SessionFactory() as session:
        _sync_tenant_workbook(session)
        tenants = list(
            session.scalars(
                select(Tenant).order_by(Tenant.unit_number)
            )
        )
        ledgers = list(
            session.scalars(
                select(MonthlyLedger).where(
                    MonthlyLedger.billing_month == billing_month
                )
            )
        )
        tenant_by_id = {tenant.id: tenant for tenant in tenants}
        ledger_rows = [
            _ledger_dict(ledger, tenant_by_id[ledger.tenant_id])
            for ledger in ledgers
            if ledger.tenant_id in tenant_by_id
        ]
        transaction_count = session.scalar(
            select(PaymentTransaction.id).limit(1)
        )
        matched_payments = list(
            session.scalars(
                select(PaymentTransaction).where(
                    PaymentTransaction.tenant_id.is_not(None)
                )
            )
        )
        due_total = sum(
            (Decimal(row["net_due"]) for row in ledger_rows), Decimal("0.00")
        )
        electricity_bill = session.scalar(
            select(KSEBMonthlyBill)
            .where(KSEBMonthlyBill.billing_month == billing_month)
            .order_by(KSEBMonthlyBill.fetched_at.desc())
            .limit(1)
        )
        electricity_bills = list(
            session.scalars(
                select(KSEBConsumerMonthlyBill)
                .where(KSEBConsumerMonthlyBill.billing_month == billing_month)
                .order_by(KSEBConsumerMonthlyBill.consumer_number)
            )
        )
        return {
            "month": billing_month.strftime("%Y-%m"),
            "tenants": [_tenant_dict(tenant) for tenant in tenants],
            "ledgers": ledger_rows,
            "summary": {
                "tenant_count": sum(tenant.is_active for tenant in tenants),
                "ledger_count": len(ledger_rows),
                "total_due": str(due_total),
                "matched_payment_count": len(matched_payments),
                "has_transactions": transaction_count is not None,
            },
            "kseb_bill": _kseb_bill_dict(electricity_bill) if electricity_bill else None,
            "kseb_bills": [_kseb_consumer_bill_dict(bill) for bill in electricity_bills],
        }


@app.get("/api/kseb/solar-workbook", include_in_schema=False)
def download_solar_workbook() -> FileResponse:
    """Refresh and download the solar usage and bill workbook."""
    try:
        with SessionFactory() as session:
            workbook_path = export_solar_workbook(session, SOLAR_WORKBOOK_PATH)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not update the solar workbook. Close it in Excel and try again.",
        ) from exc
    return FileResponse(
        workbook_path,
        filename=workbook_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/kseb/import", status_code=201)
async def import_kseb_bill(request: Request, filename: str = "kseb-bill.pdf") -> dict:
    """Import a downloaded KSEB monthly bill PDF into the local dashboard."""
    if not filename.casefold().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Choose a KSEB bill PDF.")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="KSEB bill PDFs must be 10 MB or smaller.")
    chunks = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="KSEB bill PDFs must be 10 MB or smaller.")
        chunks.append(chunk)
    pdf_bytes = b"".join(chunks)
    try:
        parsed = parse_kseb_bill_pdf(pdf_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with SessionFactory() as session:
        bill = save_kseb_bill(session, parsed, "upload")
        return _kseb_bill_dict(bill)


@app.post("/api/kseb/fetch")
def fetch_kseb_bill() -> dict:
    """Sign in once, fetch each linked service bill, and save monthly totals."""
    settings = get_settings()
    if not all((settings.kseb_wss_user_id, settings.kseb_wss_password)):
        raise HTTPException(
            status_code=503,
            detail=(
                "Configure KSEB_WSS_USER_ID and KSEB_WSS_PASSWORD in the local .env file first."
            ),
        )
    try:
        configured_numbers = ",".join(
            filter(None, (settings.kseb_consumer_numbers, settings.kseb_consumer_number))
        )
        downloaded, failures = fetch_latest_kseb_bill_pdfs(
            settings.kseb_wss_user_id,
            settings.kseb_wss_password,
            configured_numbers,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("KSEB WSS retrieval failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="KSEB bill retrieval failed. Check the local browser setup or import the PDF manually.",
        ) from exc
    saved_bills = []
    for consumer_number, pdf in downloaded.items():
        try:
            parsed = parse_kseb_bill_pdf(pdf)
            if parsed.consumer_number != consumer_number:
                raise ValueError("The downloaded bill's service number did not match.")
            with SessionFactory() as session:
                save_kseb_bill(session, parsed, "wss")
            saved_bills.append(parsed)
        except (RuntimeError, ValueError) as exc:
            failures.append((consumer_number, str(exc)))
    if not saved_bills and failures:
        raise HTTPException(
            status_code=502,
            detail=(
                f"No KSEB bills were imported. {len(failures)} linked service(s) failed; "
                "check that bills are available in Billing History."
            ),
        )
    return {
        "fetched_count": len(saved_bills),
        "failed_count": len(failures),
        "bills": [
            {
                "consumer_number": parsed.consumer_number,
                "billing_month": parsed.billing_month.strftime("%Y-%m"),
                "bill_amount": str(parsed.bill_amount),
            }
            for parsed in saved_bills
        ],
        "failures": [
            {"consumer_last_four": number[-4:], "detail": detail}
            for number, detail in failures
        ],
    }


@app.post("/api/tenants", status_code=201)
def create_tenant(payload: TenantCreate) -> dict:
    """Add a tenant to the workbook and immediately sync the registry."""
    with SessionFactory() as session:
        _sync_tenant_workbook(session)
        existing = session.scalar(
            select(Tenant).where(Tenant.unit_number == payload.unit_number)
        )
        if existing is not None:
            raise HTTPException(
                status_code=409, detail="That unit number is already registered."
            )
        values = payload.model_dump()
        values["email"] = values["email"] or None
        values["upi_vpa"] = values["upi_vpa"] or None
        values["expected_sender_name"] = values["expected_sender_name"] or None
        values["expected_remarks"] = values["expected_remarks"] or None
        try:
            upsert_tenant_workbook_row(values, TENANT_WORKBOOK_PATH)
            _sync_tenant_workbook(session)
        except (OSError, ValueError) as exc:
            logger.exception("Unable to save tenant to workbook")
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Could not save {TENANT_WORKBOOK_PATH.name}. Close it in Excel, "
                    "check its Tenants sheet, and try again."
                ),
            ) from exc
        tenant = session.scalar(
            select(Tenant).where(Tenant.unit_number == payload.unit_number)
        )
        if tenant is None:
            raise HTTPException(status_code=500, detail="Tenant was not added to the registry.")
        return _tenant_dict(tenant)


@app.post("/api/ledgers/generate")
def generate_ledgers(month: str | None = None) -> dict:
    """Generate monthly bills and return the calculated ledger rows."""
    billing_month = _parse_month(month)
    with SessionFactory() as session:
        _sync_tenant_workbook(session)
        ledgers = generate_monthly_ledgers(session, billing_month)
        tenants = {
            tenant.id: tenant
            for tenant in session.scalars(
                select(Tenant).where(Tenant.is_active.is_(True))
            )
        }
        return {
            "count": len(ledgers),
            "ledgers": [
                _ledger_dict(ledger, tenants[ledger.tenant_id])
                for ledger in ledgers
                if ledger.tenant_id in tenants
            ],
        }


@app.post("/api/payments")
def add_payment(payload: PaymentCreate) -> dict:
    """Reconcile one simulated or manually entered incoming payment."""
    payment = IncomingPayment(**payload.model_dump())
    with SessionFactory() as session:
        _sync_tenant_workbook(session)
        try:
            result = reconcile_payments(session, [payment])[0]
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="Payment could not be saved.") from exc
        return {
            "reference": result.reference,
            "tenant_id": result.tenant_id,
            "status": result.status,
            "amount": str(result.amount),
        }


@app.post("/api/reminders/preview")
def preview_reminders(month: str | None = None) -> dict:
    """Return message previews without sending notifications."""
    billing_month = _parse_month(month)
    with SessionFactory() as session:
        _sync_tenant_workbook(session)
        messages = send_monthly_reminders(session, billing_month, dry_run=True)
        return {
            "count": len(messages),
            "reminders": [
                {"tenant_id": tenant_id, "message": message}
                for tenant_id, message in messages
            ],
        }


def _parse_month(value: str | None) -> date:
    if not value:
        return month_start(datetime.now().date())
    try:
        return date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Month must use YYYY-MM format.") from exc
