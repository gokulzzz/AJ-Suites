"""Command-line interface for rental payment reconciliation and billing."""

import argparse
import logging
from datetime import date
from pathlib import Path

from rental_automation.config import get_settings
from rental_automation.kseb import (
    fetch_latest_kseb_bill_pdfs,
    parse_kseb_bill_pdf,
    save_kseb_bill,
)
from rental_automation.ledger import generate_monthly_ledgers
from rental_automation.models import create_database, initialize_database
from rental_automation.notification_service import send_monthly_reminders
from rental_automation.reconciliation import StatementFileAdapter, reconcile_payments
from rental_automation.scheduler import start_scheduler
from rental_automation.tenant_registry import (
    export_tenants_workbook,
    import_tenants,
    import_tenants_workbook,
)
logger = logging.getLogger("rental_automation")


def parse_month(value: str | None) -> date:
    """Parse YYYY-MM, defaulting to the current month."""
    if value is None:
        return date.today().replace(day=1)
    try:
        return date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Billing month must use YYYY-MM format") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-reconciliation", metavar="STATEMENT", type=Path,
        help="Import credits from a CSV or XLSX bank statement",
    )
    parser.add_argument(
        "--load-tenants", metavar="CSV", type=Path,
        help="Create or update tenants from a CSV registry",
    )
    parser.add_argument(
        "--import-kseb-bill", metavar="PDF", type=Path,
        help="Import a downloaded KSEB monthly bill PDF",
    )
    parser.add_argument(
        "--fetch-kseb", action="store_true",
        help="Fetch and save the configured generator bill from KSEB WSS",
    )
    parser.add_argument(
        "--generate-dues", nargs="?", const="current", metavar="YYYY-MM",
        help="Generate monthly ledgers for YYYY-MM (defaults to current month)",
    )
    parser.add_argument(
        "--send-reminders", nargs="?", const="current", metavar="YYYY-MM",
        help="Send reminders for YYYY-MM (defaults to current month)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without database or delivery changes")
    parser.add_argument("--scheduler", action="store_true", help="Run the monthly scheduler")
    parser.add_argument("--web", action="store_true", help="Serve the local browser dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Web server bind address")
    parser.add_argument("--port", type=int, default=8000, help="Web server port")
    return parser


def _month_argument(value: str | None) -> date:
    return parse_month(None if value in (None, "current") else value)


def main() -> None:
    """Execute requested CLI actions."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    if args.web:
        import uvicorn

        uvicorn.run("rental_automation.web:app", host=args.host, port=args.port)
        return
    if args.scheduler:
        start_scheduler()
        return
    if args.fetch_kseb:
        settings = get_settings()
        if not all((settings.kseb_wss_user_id, settings.kseb_wss_password)):
            raise SystemExit(
                "Set KSEB_WSS_USER_ID and KSEB_WSS_PASSWORD in .env first."
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
            raise SystemExit(f"KSEB fetch stopped: {exc}") from exc
        except Exception as exc:
            raise SystemExit(
                f"KSEB fetch failed ({type(exc).__name__}); see README for browser setup."
            ) from exc
        engine, session_factory = create_database()
        initialize_database(engine)
        saved_count = 0
        for consumer_number, pdf in downloaded.items():
            try:
                parsed = parse_kseb_bill_pdf(pdf)
                if parsed.consumer_number != consumer_number:
                    raise ValueError("The downloaded bill's service number did not match.")
                with session_factory() as session:
                    bill = save_kseb_bill(session, parsed, "wss")
                saved_count += 1
            except ValueError as exc:
                failures.append((consumer_number, str(exc)))
        print(f"Imported {saved_count} KSEB service bill(s).")
        print(f"Failed services: {len(failures)}.")
        for consumer_number, detail in failures:
            print(f"Service ending {consumer_number[-4:]}: {detail}")
        return
    if not any((
        args.run_reconciliation,
        args.load_tenants,
        args.import_kseb_bill,
        args.fetch_kseb,
        args.generate_dues is not None,
        args.send_reminders is not None,
    )):
        build_parser().print_help()
        return

    engine, session_factory = create_database()
    initialize_database(engine)
    with session_factory() as session:
        if not (args.load_tenants and args.dry_run):
            import_tenants_workbook(session, commit=not args.dry_run)
        if args.load_tenants:
            count = import_tenants(session, args.load_tenants, dry_run=args.dry_run)
            print(f"{'Would load' if args.dry_run else 'Loaded'} {count} tenant records")
            if not args.dry_run:
                export_tenants_workbook(session)
        if args.import_kseb_bill:
            try:
                parsed = parse_kseb_bill_pdf(args.import_kseb_bill.read_bytes())
            except (OSError, ValueError) as exc:
                raise SystemExit(f"Could not import KSEB bill: {exc}") from exc
            if not args.dry_run:
                bill = save_kseb_bill(session, parsed, "upload")
                print(
                    f"Imported KSEB bill month={bill.billing_month:%Y-%m} "
                    f"amount={bill.bill_amount} source={bill.source_label}"
                )
            else:
                print(
                    f"Parsed KSEB bill month={parsed.billing_month:%Y-%m} "
                    f"amount={parsed.bill_amount}; dry-run, not saved"
                )
        if args.run_reconciliation:
            payments = list(StatementFileAdapter().read(args.run_reconciliation))
            results = reconcile_payments(session, payments, dry_run=args.dry_run)
            for result in results:
                print(
                    f"{result.status}: ref={result.reference or '-'} "
                    f"tenant={result.tenant_id or '-'} amount={result.amount}"
                )
        if args.generate_dues is not None:
            month = _month_argument(args.generate_dues)
            ledgers = generate_monthly_ledgers(session, month, dry_run=args.dry_run)
            for ledger in ledgers:
                print(
                    f"Unit tenant_id={ledger.tenant_id} month={ledger.billing_month:%Y-%m} "
                    f"due={ledger.net_due} status={ledger.status}"
                )
        if args.send_reminders is not None:
            month = _month_argument(args.send_reminders)
            messages = send_monthly_reminders(
                session, month, dry_run=args.dry_run
            )
            for tenant_id, message in messages:
                print(f"Reminder tenant_id={tenant_id}: {message}")


if __name__ == "__main__":
    main()
