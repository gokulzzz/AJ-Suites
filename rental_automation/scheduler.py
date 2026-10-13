"""Monthly billing and reminder scheduler."""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session, sessionmaker

from rental_automation.config import get_settings
from rental_automation.kseb import fetch_latest_kseb_bill_pdfs, parse_kseb_bill_pdf, save_kseb_bill
from rental_automation.ledger import generate_monthly_ledgers
from rental_automation.models import create_database, initialize_database
from rental_automation.notification_service import send_monthly_reminders
from rental_automation.tenant_registry import import_tenants_workbook

logger = logging.getLogger(__name__)


def run_monthly_cycle(
    session_factory: sessionmaker[Session], billing_month: date | None = None
) -> None:
    """Generate current ledgers and dispatch their reminders."""
    timezone = ZoneInfo(get_settings().timezone)
    billing_month = billing_month or datetime.now(timezone).date().replace(day=1)
    with session_factory() as session:
        import_tenants_workbook(session)
        ledgers = generate_monthly_ledgers(session, billing_month)
        logger.info("Generated/loaded %d ledgers for %s", len(ledgers), billing_month)
        send_monthly_reminders(session, billing_month)


def run_monthly_kseb_fetch(session_factory: sessionmaker[Session]) -> None:
    """Fetch monthly bills for every KSEB service linked to the WSS account."""
    settings = get_settings()
    if not all((settings.kseb_wss_user_id, settings.kseb_wss_password)):
        logger.info(
            "Skipping scheduled KSEB fetch: configure the WSS user ID and password in .env"
        )
        return
    try:
        configured_numbers = ",".join(
            filter(None, (settings.kseb_consumer_numbers, settings.kseb_consumer_number))
        )
        downloaded, failures = fetch_latest_kseb_bill_pdfs(
            settings.kseb_wss_user_id,
            settings.kseb_wss_password,
            configured_numbers,
        )
        saved_count = 0
        for consumer_number, pdf in downloaded.items():
            try:
                parsed = parse_kseb_bill_pdf(pdf)
                if parsed.consumer_number != consumer_number:
                    raise ValueError("The downloaded bill's service number did not match.")
                with session_factory() as session:
                    save_kseb_bill(session, parsed, "wss")
                saved_count += 1
            except (RuntimeError, ValueError) as exc:
                failures.append((consumer_number, str(exc)))
        logger.info("Saved monthly KSEB bills for %d consumer service(s)", saved_count)
        for consumer_number, error in failures:
            logger.warning("KSEB fetch failed for service ending %s: %s", consumer_number[-4:], error)
    except RuntimeError as exc:
        logger.warning("Scheduled KSEB fetch stopped: %s", exc)
    except Exception as exc:
        logger.warning("Scheduled KSEB fetch failed (%s)", type(exc).__name__)


def start_scheduler() -> None:
    """Run rent reminders on day 2 and KSEB retrieval on the configured day."""
    settings = get_settings()
    engine, session_factory = create_database()
    initialize_database(engine)
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(
        run_monthly_cycle,
        trigger="cron",
        day=2,
        hour=9,
        minute=0,
        args=[session_factory],
        id="monthly-rent-reminders",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_monthly_kseb_fetch,
        trigger="cron",
        day=settings.kseb_fetch_day,
        hour=9,
        minute=0,
        args=[session_factory],
        id="monthly-kseb-bill-fetch",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    logger.info(
        "Starting monthly scheduler: rent reminders day 2, KSEB fetch day %d at 09:00 (%s)",
        settings.kseb_fetch_day,
        settings.timezone,
    )
    scheduler.start()
