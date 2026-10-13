"""Token and PDF helpers for online rental agreements."""

import hashlib
import io
import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from pathlib import Path


def create_signing_token() -> str:
    """Return an unguessable bearer token for one agreement party."""
    return secrets.token_urlsafe(32)


def hash_signing_token(token: str) -> str:
    """Store only a SHA-256 digest of the high-entropy signing token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def agreement_terms_hash(agreement, occupants) -> str:
    """Hash the agreement's immutable terms and occupant contact snapshots."""
    snapshot = {
        "agreement_code": agreement.agreement_code,
        "tenant": [agreement.tenant_name, agreement.tenant_phone, agreement.tenant_email],
        "landlord": [agreement.landlord_name, agreement.landlord_phone, agreement.landlord_email],
        "property_address": agreement.property_address,
        "check_in_date": agreement.check_in_date.isoformat(),
        "rental_start_date": agreement.rental_start_date.isoformat(),
        "rental_end_date": agreement.rental_end_date.isoformat(),
        "monthly_rent": format(Decimal(agreement.monthly_rent), ".2f"),
        "security_deposit": format(Decimal(agreement.security_deposit), ".2f"),
        "rent_due_day": agreement.rent_due_day,
        "additional_terms": agreement.additional_terms,
        "occupants": [
            [item.name, item.phone, item.email, item.relationship_to_tenant]
            for item in occupants
        ],
    }
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stamp(value: datetime | None) -> str:
    if value is None:
        return "Not signed"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_agreement_pdf(agreement) -> bytes:
    """Build a readable PDF of the terms and the recorded signatures."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise RuntimeError("Install project dependencies to create agreement PDFs.") from exc

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Rental agreement {agreement.agreement_code}",
        author="Rental Payment Desk",
    )
    font_name = "Helvetica"
    font_candidates = [
        (Path("C:/Windows/Fonts/Nirmala.ttf"), Path("C:/Windows/Fonts/NirmalaB.ttf")),
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"), Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    ]
    for regular_path, bold_path in font_candidates:
        if regular_path.is_file():
            try:
                pdfmetrics.registerFont(TTFont("AgreementSans", str(regular_path)))
                bold_font = "AgreementSans-Bold"
                if bold_path.is_file():
                    pdfmetrics.registerFont(TTFont(bold_font, str(bold_path)))
                else:
                    bold_font = "AgreementSans"
                pdfmetrics.registerFontFamily(
                    "AgreementSans", normal="AgreementSans", bold=bold_font,
                    italic="AgreementSans", boldItalic=bold_font,
                )
                font_name = "AgreementSans"
                break
            except Exception:
                continue
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AgreementTitle", parent=styles["Title"], alignment=TA_CENTER,
        fontName=font_name, fontSize=17, leading=21,
        textColor=colors.HexColor("#155f4b"), spaceAfter=8,
    )
    heading_style = ParagraphStyle(
        "AgreementHeading", parent=styles["Heading2"], fontSize=11, leading=14,
        fontName=font_name, textColor=colors.HexColor("#155f4b"), spaceBefore=10, spaceAfter=5,
    )
    body_style = ParagraphStyle("AgreementBody", parent=styles["BodyText"], fontName=font_name, fontSize=9, leading=13)
    small_style = ParagraphStyle("AgreementSmall", parent=body_style, fontName=font_name, fontSize=8, leading=11)

    def para(value, style=body_style):
        return Paragraph(escape(str(value or "")).replace("\n", "<br/>"), style)

    def rich(value, style=body_style):
        return Paragraph(value, style)

    def field(label, value):
        return [rich(f"<b>{escape(label)}</b>"), para(value)]

    story = [para("RESIDENTIAL RENTAL AGREEMENT", title_style)]
    story.append(para(f"Agreement reference: {escape(agreement.agreement_code)}", small_style))
    story.append(para(
        "The parties record the following rental terms for the property and period set out below.",
        body_style,
    ))
    story.append(para("Parties and property", heading_style))
    rows = [
        field("Landlord", f"{agreement.landlord_name} | {agreement.landlord_phone}" + (f" | {agreement.landlord_email}" if agreement.landlord_email else "")),
        field("Main tenant", f"{agreement.tenant_name} | {agreement.tenant_phone}" + (f" | {agreement.tenant_email}" if agreement.tenant_email else "")),
        field("Rental property", agreement.property_address),
    ]
    table_data = [[left, right] for pair in rows for left, right in [pair]]
    table = Table(table_data, colWidths=[38 * mm, 128 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f5f2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#dce5df")),
        ("INNERGRID", (0, 0), (-1, -1), .3, colors.HexColor("#e6ece8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([table, para("Dates and payment terms", heading_style)])
    term_rows = [
        [rich("<b>Check-in date</b>"), para(agreement.check_in_date.strftime("%d %B %Y")), rich("<b>Rental period</b>"), para(f"{agreement.rental_start_date:%d %B %Y} to {agreement.rental_end_date:%d %B %Y}")],
        [rich("<b>Monthly rent</b>"), para(f"INR {agreement.monthly_rent:,.2f}"), rich("<b>Security deposit</b>"), para(f"INR {agreement.security_deposit:,.2f}")],
        [rich("<b>Rent due</b>"), para(f"By day {agreement.rent_due_day} of each month"), rich("<b>Status</b>"), para(agreement.status.replace("_", " ").title())],
    ]
    terms_table = Table(term_rows, colWidths=[29 * mm, 54 * mm, 31 * mm, 52 * mm], hAlign="LEFT")
    terms_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f5f2")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f0f5f2")),
        ("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#dce5df")),
        ("INNERGRID", (0, 0), (-1, -1), .3, colors.HexColor("#e6ece8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(terms_table)

    story.append(para("Other residents", heading_style))
    if agreement.occupants:
        occupants = [[rich("<b>Name</b>"), rich("<b>Relationship</b>"), rich("<b>Phone</b>"), rich("<b>Email</b>")]]
        occupants.extend([
            [para(item.name), para(item.relationship_to_tenant or "-"), para(item.phone), para(item.email or "-")]
            for item in agreement.occupants
        ])
        occupant_table = Table(occupants, colWidths=[49 * mm, 35 * mm, 39 * mm, 43 * mm], repeatRows=1)
        occupant_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f5f2")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#dce5df")),
            ("INNERGRID", (0, 0), (-1, -1), .3, colors.HexColor("#e6ece8")),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(occupant_table)
    else:
        story.append(para("No additional residents listed."))

    story.append(para("Additional terms", heading_style))
    story.append(para(agreement.additional_terms or "No additional terms supplied."))
    story.append(para("Electronic signing record", heading_style))
    signature_cells = []
    for role, signer, phone, signed_at, signature in (
        ("Tenant", agreement.tenant_signed_name, agreement.tenant_phone, agreement.tenant_signed_at, agreement.tenant_signature_png),
        ("Landlord", agreement.landlord_signed_name, agreement.landlord_phone, agreement.landlord_signed_at, agreement.landlord_signature_png),
    ):
        if signature:
            try:
                image = Image(io.BytesIO(signature), width=48 * mm, height=18 * mm, kind="proportional")
            except Exception:
                image = para("Signature image unavailable", small_style)
        else:
            image = para("Awaiting signature", small_style)
        detail = rich(f"<b>{escape(role)}:</b> {escape(signer or 'Not signed')}<br/>{escape(phone)}<br/>{escape(_stamp(signed_at))}", small_style)
        signature_cells.append([image, detail])
    signature_table = Table(signature_cells, colWidths=[60 * mm, 106 * mm], rowHeights=[23 * mm, 23 * mm])
    signature_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#dce5df")),
        ("INNERGRID", (0, 0), (-1, -1), .3, colors.HexColor("#e6ece8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([signature_table, Spacer(1, 8)])
    story.append(para(
        "Signing method: each signer typed their name, agreed to the displayed terms, and submitted a drawn signature image. "
        "This record is not an Aadhaar eSign or a Digital Signature Certificate (DSC), and it does not independently verify identity. "
        "The parties are responsible for checking applicable stamp duty, execution, and registration requirements for this agreement.",
        small_style,
    ))
    document.build(story)
    return output.getvalue()
