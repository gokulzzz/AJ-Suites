"""KSEB WSS bill retrieval, PDF parsing, and monthly persistence."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from rental_automation.models import KSEBConsumerMonthlyBill, KSEBMonthlyBill
from rental_automation.solar_workbook import export_solar_workbook

WSS_HOME = "https://wss.kseb.in/"
WSS_LOGIN = "https://wss.kseb.in/selfservices/wssloginUser.do"
WSS_HISTORY = "https://wss.kseb.in/selfservices/HistoryPaysBills"
_ZONES = ("normal", "off_peak", "peak")
_NUMBER = r"([\d,]+(?:\.\d+)?)"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedKSEBBill:
    """Readings and official charge extracted from a KSEB monthly PDF."""

    consumer_number: str
    billing_month: date
    billing_date: date | None
    imported_normal_kwh: Decimal
    imported_off_peak_kwh: Decimal
    imported_peak_kwh: Decimal
    exported_normal_kwh: Decimal
    exported_off_peak_kwh: Decimal
    exported_peak_kwh: Decimal
    solar_normal_kwh: Decimal
    solar_off_peak_kwh: Decimal
    solar_peak_kwh: Decimal
    bill_amount: Decimal


def parse_kseb_bill_pdf(pdf_bytes: bytes) -> ParsedKSEBBill:
    """Extract the generator readings and payable amount from a WSS bill PDF."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("The uploaded file is not a PDF.")
    if len(pdf_bytes) > 10 * 1024 * 1024:
        raise ValueError("KSEB bill PDFs must be 10 MB or smaller.")
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        raise ValueError("Could not read the KSEB PDF. Download a fresh bill PDF and retry.") from exc
    if not pages:
        raise ValueError("This does not look like a complete KSEB monthly bill PDF.")

    first_pages = "\n".join(pages[:2])
    all_pages = "\n".join(pages)
    period_match = re.search(
        r"Billing\s+Period\s*(\d{1,2})\s*/\s*(\d{4})", first_pages, re.IGNORECASE
    )
    if not period_match:
        period_match = re.search(
            r"Demand\s+for\s+(\d{1,2})\s*/\s*(\d{4})", first_pages, re.IGNORECASE
        )
    if not period_match:
        raise ValueError("Could not find the billing month in this KSEB PDF.")
    month, year = map(int, period_match.groups())
    try:
        billing_month = date(year, month, 1)
    except ValueError as exc:
        raise ValueError("The KSEB bill contains an invalid billing month.") from exc

    consumer_match = re.search(r"Consumer\s*#\s*(\d{13})", first_pages, re.IGNORECASE)
    if not consumer_match:
        raise ValueError("Could not identify the generator consumer number in this bill.")
    consumer_number = consumer_match.group(1)

    readings: dict[tuple[str, str], Decimal] = {}
    meter_rows = re.compile(
        rf"KWH\s+(Normal|Off\s+Peak|Peak)\s+(Import|Export)\s+"
        rf"{_NUMBER}\s+{_NUMBER}\s+{_NUMBER}\s+{_NUMBER}",
        re.IGNORECASE,
    )
    for page_text in pages:
        for match in meter_rows.finditer(page_text):
            zone = match.group(1).casefold().replace(" ", "_")
            direction = match.group(2).casefold()
            readings[(direction, zone)] = _decimal(match.group(6))
    cumulative_meter_rows = re.compile(
        rf"\bKWH\s+Cumulative\s+(Import|Export)\s+"
        rf"{_NUMBER}\s+{_NUMBER}\s+{_NUMBER}\s+{_NUMBER}",
        re.IGNORECASE,
    )
    for page_text in pages:
        for match in cumulative_meter_rows.finditer(page_text):
            direction = match.group(1).casefold()
            readings.setdefault((direction, "normal"), _decimal(match.group(5)))
    if not any(direction == "import" for direction, _ in readings):
        raise ValueError("Could not find electricity import readings in this KSEB PDF.")
    for direction in ("import", "export"):
        for zone in _ZONES:
            readings.setdefault((direction, zone), Decimal("0"))

    solar: dict[str, Decimal] = {}
    solar_rows = re.compile(
        rf"\b\d{{2}}[-/]\d{{2}}[-/]\d{{4}}\s+W(NL|OP|PK)\s+\S+\s+"
        rf"{_NUMBER}\s+{_NUMBER}\s+{_NUMBER}\s+{_NUMBER}",
        re.IGNORECASE,
    )
    for page_text in pages:
        for match in solar_rows.finditer(page_text):
            zone = {"NL": "normal", "OP": "off_peak", "PK": "peak"}[match.group(1).upper()]
            solar[zone] = _decimal(match.group(5))
    amount_match = re.search(
        rf"Net\s+Payable(?:\s*\([^)]*\))?\s*{_NUMBER}",
        all_pages,
        re.IGNORECASE,
    )
    if not amount_match:
        raise ValueError("Could not find KSEB's net payable amount in this bill.")

    billing_date = _read_billing_date(first_pages)
    return ParsedKSEBBill(
        consumer_number=consumer_number,
        billing_month=billing_month,
        billing_date=billing_date,
        imported_normal_kwh=readings[("import", "normal")],
        imported_off_peak_kwh=readings[("import", "off_peak")],
        imported_peak_kwh=readings[("import", "peak")],
        exported_normal_kwh=readings[("export", "normal")],
        exported_off_peak_kwh=readings[("export", "off_peak")],
        exported_peak_kwh=readings[("export", "peak")],
        solar_normal_kwh=solar.get("normal", Decimal("0")),
        solar_off_peak_kwh=solar.get("off_peak", Decimal("0")),
        solar_peak_kwh=solar.get("peak", Decimal("0")),
        bill_amount=_decimal(amount_match.group(1)),
    )


def save_kseb_bill(
    session: Session, parsed: ParsedKSEBBill, source: str
) -> KSEBMonthlyBill:
    """Upsert one service bill, then refresh the property's monthly aggregate."""
    service_bill = session.scalar(
        select(KSEBConsumerMonthlyBill).where(
            KSEBConsumerMonthlyBill.billing_month == parsed.billing_month,
            KSEBConsumerMonthlyBill.consumer_number == parsed.consumer_number,
        )
    )
    if service_bill is None:
        service_bill = KSEBConsumerMonthlyBill(
            consumer_number=parsed.consumer_number,
            billing_month=parsed.billing_month,
            imported_normal_kwh=Decimal("0"),
            imported_off_peak_kwh=Decimal("0"),
            imported_peak_kwh=Decimal("0"),
            exported_normal_kwh=Decimal("0"),
            exported_off_peak_kwh=Decimal("0"),
            exported_peak_kwh=Decimal("0"),
            solar_normal_kwh=Decimal("0"),
            solar_off_peak_kwh=Decimal("0"),
            solar_peak_kwh=Decimal("0"),
            bill_amount=Decimal("0"),
            source_label="Uploaded PDF",
            fetched_at=datetime.now().astimezone(),
        )
        session.add(service_bill)

    value_fields = (
        "billing_date",
        "imported_normal_kwh",
        "imported_off_peak_kwh",
        "imported_peak_kwh",
        "exported_normal_kwh",
        "exported_off_peak_kwh",
        "exported_peak_kwh",
        "solar_normal_kwh",
        "solar_off_peak_kwh",
        "solar_peak_kwh",
        "bill_amount",
    )
    for field in value_fields:
        setattr(service_bill, field, getattr(parsed, field))
    service_bill.source_label = "KSEB WSS" if source.casefold() == "wss" else "Uploaded PDF"
    service_bill.fetched_at = datetime.now().astimezone()
    session.flush()

    service_bills = list(
        session.scalars(
            select(KSEBConsumerMonthlyBill).where(
                KSEBConsumerMonthlyBill.billing_month == parsed.billing_month
            )
        )
    )
    bill = session.scalar(
        select(KSEBMonthlyBill).where(
            KSEBMonthlyBill.billing_month == parsed.billing_month,
        )
    )
    if bill is None:
        bill = KSEBMonthlyBill(
            billing_month=parsed.billing_month,
            imported_normal_kwh=Decimal("0"),
            imported_off_peak_kwh=Decimal("0"),
            imported_peak_kwh=Decimal("0"),
            exported_normal_kwh=Decimal("0"),
            exported_off_peak_kwh=Decimal("0"),
            exported_peak_kwh=Decimal("0"),
            solar_normal_kwh=Decimal("0"),
            solar_off_peak_kwh=Decimal("0"),
            solar_peak_kwh=Decimal("0"),
            bill_amount=Decimal("0"),
            source_label="Uploaded PDF",
            fetched_at=datetime.now().astimezone(),
        )
        session.add(bill)

    for field in value_fields[1:]:
        setattr(bill, field, sum((getattr(item, field) for item in service_bills), Decimal("0")))
    bill.billing_date = max(
        (item.billing_date for item in service_bills if item.billing_date), default=None
    )
    bill_sources = {item.source_label for item in service_bills}
    bill.source_label = next(iter(bill_sources)) if len(bill_sources) == 1 else "Mixed sources"
    latest_fetched_item = max(
        service_bills, key=lambda item: _datetime_order(item.fetched_at)
    )
    bill.fetched_at = latest_fetched_item.fetched_at
    session.commit()
    session.refresh(bill)
    try:
        export_solar_workbook(session)
    except Exception as exc:
        # Keep a successfully saved bill available even if the workbook is open or locked.
        logger.warning("Could not refresh solar_consumption.xlsx (%s)", type(exc).__name__)
    return bill


def fetch_latest_kseb_bill_pdfs(
    user_id: str,
    password: str,
    configured_consumer_numbers: str | tuple[str, ...] | list[str] = (),
) -> tuple[dict[str, bytes], list[tuple[str, str]]]:
    """Sign in once and download the latest available bill for each linked service.

    Consumer services shown in Billing History are discovered automatically. Optional
    configured service numbers are added as a fallback. CAPTCHA and OTP challenges stop
    retrieval; no browser profile or credentials are persisted.
    """
    if not user_id.strip() or not password:
        raise RuntimeError("Set KSEB_WSS_USER_ID and KSEB_WSS_PASSWORD in the local .env file.")
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        str(Path(__file__).resolve().parents[1] / ".playwright-browsers"),
    )
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Install the KSEB browser dependency with 'pip install -r requirements.txt' "
            "and 'playwright install chromium'."
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            try:
                # The site root redirects to a public landing page; use the actual
                # WSS user ID/password form so we submit the intended login flow.
                page.goto(WSS_LOGIN, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "Timed out loading the KSEB WSS sign-in page. Check this computer's access to wss.kseb.in."
                ) from exc
            try:
                page.locator("input[type='password']").wait_for(
                    state="visible", timeout=20000
                )
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "KSEB sign-in fields changed; bill retrieval stopped safely."
                ) from exc
            _dismiss_kseb_login_notice(page)
            login_form = page.locator("form[action$='/login']")
            user_input = login_form.locator("input[name='userId']")
            password_input = login_form.locator("input[name='passWord']")
            if not login_form.count() or not user_input.is_visible() or not password_input.is_visible():
                raise RuntimeError("KSEB sign-in fields changed; bill retrieval stopped safely.")
            user_input.fill(user_id)
            password_input.fill(password)
            if _has_verification_challenge(page):
                raise RuntimeError(
                    "KSEB requires a verification step. Automated retrieval stopped before sign-in."
                )
            sign_in = login_form.get_by_role(
                "button", name=re.compile(r"sign\s*in|login", re.I)
            )
            sign_in_button = next(
                (
                    sign_in.nth(index)
                    for index in range(sign_in.count())
                    if sign_in.nth(index).is_visible()
                ),
                None,
            )
            if sign_in_button is None:
                raise RuntimeError("KSEB sign-in button was not found; bill retrieval stopped safely.")
            _dismiss_kseb_login_notice(page)
            try:
                sign_in_button.click(timeout=8000, no_wait_after=True)
            except PlaywrightTimeoutError as exc:
                try:
                    password_input.press("Enter", timeout=5000)
                except PlaywrightTimeoutError:
                    raise RuntimeError(
                        "KSEB sign-in form could not be submitted through its button or keyboard."
                    ) from exc
            _wait_for_kseb_login(page)

            consumer_numbers = set(_normalize_consumer_numbers(configured_consumer_numbers))
            # WSS asynchronously renders the linked-consumer selectors on its
            # authenticated landing page. Discover them before navigating away.
            for _ in range(20):
                consumer_numbers.update(_discover_consumer_numbers(page))
                if consumer_numbers:
                    break
                page.wait_for_timeout(500)

            if not consumer_numbers:
                raise RuntimeError(
                    "No consumer services were found in the authenticated KSEB account. "
                    "Set KSEB_CONSUMER_NUMBERS in .env to list them explicitly."
                )

            downloaded: dict[str, bytes] = {}
            failures: list[tuple[str, str]] = []
            for consumer_number in sorted(consumer_numbers):
                try:
                    pdf = _download_latest_kseb_bill_from_history(
                        context, consumer_number
                    )
                    parsed = parse_kseb_bill_pdf(pdf)
                    if parsed.consumer_number != consumer_number:
                        raise RuntimeError(
                            "KSEB returned a bill for a different service. The PDF was discarded."
                        )
                    downloaded[consumer_number] = pdf
                except Exception as exc:
                    message = str(exc).strip() if isinstance(exc, (RuntimeError, ValueError)) else "Bill download failed."
                    failures.append((consumer_number, message or "Bill download failed."))
            return downloaded, failures
        finally:
            context.close()
            browser.close()


def fetch_latest_kseb_bill_pdf(user_id: str, password: str, consumer_number: str) -> bytes:
    """Compatibility wrapper for callers that request a single service bill."""
    normalized = _normalize_consumer_numbers([consumer_number])
    if not normalized:
        raise RuntimeError("Set a valid 13-digit consumer number in the local .env file.")
    downloaded, failures = fetch_latest_kseb_bill_pdfs(user_id, password, normalized)
    if normalized[0] in downloaded:
        return downloaded[normalized[0]]
    failure = next((reason for number, reason in failures if number == normalized[0]), None)
    raise RuntimeError(failure or "KSEB did not return a bill for the requested service.")


def _normalize_consumer_numbers(values: str | tuple[str, ...] | list[str]) -> list[str]:
    entries = values.split(",") if isinstance(values, str) else values
    normalized = set()
    for entry in entries:
        number = re.sub(r"\D", "", str(entry))
        if len(number) == 13:
            normalized.add(number)
    return sorted(normalized)


def _discover_consumer_numbers(page) -> set[str]:
    """Read 13-digit service IDs exposed by WSS account selectors and bill rows."""
    found: set[str] = set()
    for select in page.locator("select").all():
        try:
            for option in select.locator("option").all():
                found.update(_numbers_in_text(option.inner_text(timeout=1000)))
                found.update(_numbers_in_text(option.get_attribute("value") or ""))
        except Exception:
            continue
    for row in page.locator("tr").all():
        try:
            found.update(_numbers_in_text(row.inner_text(timeout=1000)))
        except Exception:
            continue
    if not found:
        try:
            found.update(_numbers_in_text(page.locator("body").inner_text(timeout=3000)))
        except Exception:
            pass
    return found


def _numbers_in_text(value: str) -> set[str]:
    return set(re.findall(r"(?<!\d)\d{13}(?!\d)", value or ""))


def _download_latest_kseb_bill_from_history(context, consumer_number: str) -> bytes:
    """Use WSS's authenticated history requests to retrieve one latest PDF."""
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": WSS_HISTORY,
    }
    check = context.request.post(
        urljoin(WSS_HISTORY, "billHistorycheck"),
        form={"optionVal": consumer_number},
        headers=headers,
        timeout=15000,
    )
    if not check.ok:
        raise RuntimeError(
            f"KSEB could not load history for this service (HTTP {check.status})."
        )
    try:
        check_data = check.json()
    except Exception as exc:
        raise RuntimeError("KSEB returned an unreadable bill-history response.") from exc
    if check_data is None or check_data == []:
        raise RuntimeError("KSEB has no bill history for this service.")

    history = context.request.post(
        urljoin(WSS_HISTORY, "billHistory"),
        form={"optionVal": consumer_number, "periodVall": "12"},
        headers=headers,
        timeout=15000,
    )
    if not history.ok:
        raise RuntimeError(
            f"KSEB could not load the latest bill records (HTTP {history.status})."
        )
    try:
        records = history.json()
    except Exception as exc:
        raise RuntimeError("KSEB returned unreadable bill records.") from exc
    if not isinstance(records, list):
        raise RuntimeError("KSEB returned an unexpected bill-history format.")
    eligible = [
        record
        for record in records
        if isinstance(record, dict)
        and str(record.get("consumerNumber", consumer_number)) == consumer_number
        and record.get("billTypeCode") in {"Normal", "RgCC"}
        and record.get("mbcBillNo")
    ]
    if not eligible:
        raise RuntimeError("KSEB has no downloadable monthly bill for this service.")
    latest = max(eligible, key=lambda item: str(item.get("billMonth", "")))
    pdf_url = urljoin(
        WSS_HISTORY,
        "openbillasPDF?"
        + urlencode(
            {
                "Billno": str(latest["mbcBillNo"]),
                "consumerNo": consumer_number,
            }
        ),
    )
    if urlparse(pdf_url).netloc.casefold() != "wss.kseb.in":
        raise RuntimeError("KSEB returned an unexpected bill address; the PDF was discarded.")
    response = context.request.get(pdf_url, timeout=20000)
    body = response.body()
    if not response.ok or not body.startswith(b"%PDF-"):
        raise RuntimeError("KSEB did not return a PDF for the latest bill.")
    if len(body) > 10 * 1024 * 1024:
        raise RuntimeError("KSEB returned a bill PDF larger than the 10 MB limit.")
    return body


def _has_verification_challenge(page) -> bool:
    challenge_attributes = re.compile(r"otp|captcha|verification|security.?code", re.I)
    for field in page.locator("input").all():
        if not field.is_visible():
            continue
        attributes = " ".join(
            filter(
                None,
                (
                    field.get_attribute("name"),
                    field.get_attribute("id"),
                    field.get_attribute("placeholder"),
                    field.get_attribute("aria-label"),
                ),
            )
        )
        if challenge_attributes.search(attributes):
            return True
    prompt = page.get_by_text(
        re.compile(r"(?:enter|provide|input)\s+(?:the\s+)?(?:otp|captcha|verification code)", re.I)
    )
    if any(item.is_visible() for item in prompt.all()):
        return True
    for selector in (
        "iframe[src*='captcha' i]",
        "iframe[title*='captcha' i]",
        "[class*='captcha' i]",
    ):
        if any(item.is_visible() for item in page.locator(selector).all()):
            return True
    return False


def _dismiss_kseb_login_notice(page) -> None:
    """Close KSEB's dismissible public notice when it covers the sign-in form."""
    modal = page.locator("#warningModal")
    # The notice can appear shortly after the page loads, so check briefly for it.
    for _ in range(12):
        if modal.is_visible():
            close = modal.locator("button.cancelBtn")
            if close.count() and close.first.is_visible():
                close.first.click(timeout=3000)
                modal.wait_for(state="hidden", timeout=5000)
                return
        page.wait_for_timeout(500)
    if modal.is_visible():
        raise RuntimeError(
            "KSEB's login notice is covering the sign-in form and could not be dismissed."
        )


def _wait_for_kseb_login(page) -> None:
    """Wait for WSS to finish its redirect and expose authenticated navigation."""
    for _ in range(30):
        if _has_verification_challenge(page):
            raise RuntimeError(
                "KSEB requires a verification step. Automated retrieval stopped; no CAPTCHA or OTP was bypassed."
            )
        if not any(
            field.is_visible()
            for field in page.locator("input[type='password']").all()
        ):
            return
        for selector in ("[role='alert']", ".alert-danger", ".error-message", "[class*='error' i]"):
            for message in page.locator(selector).all():
                try:
                    if message.is_visible() and re.search(
                        r"invalid|incorrect|wrong|failed|not valid|unable to",
                        message.inner_text(timeout=500),
                        re.IGNORECASE,
                    ):
                        raise RuntimeError(
                            "KSEB rejected the login. Verify the local WSS user ID and password."
                        )
                except RuntimeError:
                    raise
                except Exception:
                    continue
        try:
            body_text = page.locator("body").inner_text(timeout=1000)
        except Exception:
            body_text = ""
        if (
            not any(
                field.is_visible()
                for field in page.locator("input[type='password']").all()
            )
            and re.search(r"\b(?:log|sign)\s*out\b", body_text, re.IGNORECASE)
        ):
            return
        page.wait_for_timeout(1000)
    raise RuntimeError(
        "KSEB did not reach an authenticated account page. Check the WSS credentials or portal status."
    )


def _first_visible(page, selectors):
    for selector in selectors:
        items = page.locator(selector)
        for index in range(items.count()):
            item = items.nth(index)
            if item.is_visible():
                return item
    return None


def _read_billing_date(text: str) -> date | None:
    match = re.search(
        r"(?:Bill\s+Date|Date\s+of\s+Bill)\s*[:#]?\s*(\d{2})[-/](\d{2})[-/](\d{4})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "")).quantize(Decimal("0.001"))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("KSEB PDF contains an invalid numeric reading.") from exc


def _datetime_order(value: datetime) -> float:
    """Compare SQLite naive timestamps alongside timezone-aware new values."""
    return (value if value.tzinfo else value.astimezone()).timestamp()


