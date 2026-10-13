# Rental payment automation

A Python 3.11+ CLI application for importing bank credits, reconciling payments to tenants, maintaining monthly rent ledgers, and previewing or sending reminders. SQLite is the default; SQLAlchemy also accepts PostgreSQL URLs.

## Setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The application creates its configured tables on startup. Configure settings through environment variables or a `.env` file:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./rental_automation.db` | SQLite or PostgreSQL connection URL |
| `TENANT_REGISTRY_MODE` | `workbook` | `workbook` keeps the local Excel file as the tenant source; `database` uses the database as the source for hosted deployments |
| `TIMEZONE` | `Asia/Kolkata` | Scheduler timezone |
| `PAYMENT_MATCH_THRESHOLD` | `0.82` | Minimum fuzzy-name/remarks match score |
| `PAYMENT_AMOUNT_TOLERANCE` | `1.00` | Reserved tolerance for amount-assisted matching |
| `NOTIFICATION_BACKEND` | `mock` | `mock`, `twilio_sms`, or `twilio_whatsapp` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | empty | Twilio credentials and sender |
| `PAYMENT_UPI_VPA` | empty | Property manager's receiving UPI VPA for generated payment intents |
| `PAYMENT_UPI_PAYEE_NAME` | `Property Manager` | Payee label in generated UPI intents |
| `REMINDER_DEADLINE_DAY` | `5` | Payment deadline day (clamped to month end) |
| `KSEB_WSS_USER_ID` | empty | KSEB Web Self Service login ID, stored only in local `.env` |
| `KSEB_WSS_PASSWORD` | empty | KSEB WSS password, stored only in local `.env` |
| `KSEB_CONSUMER_NUMBERS` | empty | Optional comma-separated 13-digit service list; linked services are discovered from Billing History |
| `KSEB_CONSUMER_NUMBER` | empty | Legacy single-service fallback |
| `KSEB_FETCH_DAY` | `15` | Day of month for the scheduled KSEB bill retrieval |
| `PUBLIC_APP_URL` | empty | Public HTTPS base URL used to build remote rental-agreement signing links |
| `APP_AUTH_USERNAME`, `APP_AUTH_PASSWORD` | empty | Manager dashboard HTTP Basic credentials, required with `PUBLIC_APP_URL` |
| `ID_PROOF_ENCRYPTION_KEY` | empty | Fernet key for encrypting occupant ID proof files; required with `PUBLIC_APP_URL` |

Do not commit real credentials or expose database files containing tenant data.

## Commands

```powershell
python -m rental_automation --run-reconciliation .\statement.csv
python -m rental_automation --load-tenants .\tenants.csv
python -m rental_automation --generate-dues 2026-10
python -m rental_automation --send-reminders 2026-10 --dry-run
python -m rental_automation --import-kseb-bill .\bill.pdf
python -m rental_automation --fetch-kseb
python -m rental_automation --scheduler
python -m rental_automation --web
pytest
```

The admin dashboard is available at `http://127.0.0.1:8000` when started with `--web`. It lets managers add tenants, generate a monthly ledger, record a test UPI credit, preview (not send) reminders, prepare rental agreements, and issue tenant onboarding links. In **Tenant onboarding**, choose the main tenant/agreement holder and unit, then generate the private link and use **Open WhatsApp message** to prepare a message to the phone number on that tenant record. Review and send the message from WhatsApp; the app does not send it automatically. The tenant page collects the main tenant's name, email, phone and optional UPI/VPA, shows the linked Flat/Room No., and lets them add each accompanying occupant with contact details. Additional occupant ID proofs can be uploaded separately. Submissions remain pending until a manager approves them; approved main-tenant contact and UPI/VPA values sync to the registry. The app binds to localhost by default. Remote access requires deploying behind HTTPS, setting `PUBLIC_APP_URL`, and setting manager credentials in `.env`; manager routes and the admin dashboard then require HTTP Basic authentication while individual capability links authorize only their tenant or signer flow.

Tenant CSV requires `name`, `unit_number`, `phone`, and `monthly_rent`; supported optional columns are `maintenance_fee`, `deposit_amount`, `opening_balance` (positive arrears, negative advance), `due_day`, `is_active`, `email`, `upi_vpa`, `expected_sender_name`, and `expected_remarks`. Imports update records by unit number. Statement input accepts CSV and XLSX with common headers such as `Date`, `Amount`/`Credit`, `UTR`/`RRN`, `UPI ID`/`VPA`, `Sender Name`, and `Narration`. Only positive amounts are ingested. `AlertTextAdapter` is available to applications integrating an SMS/email fetcher; a bank aggregator/webhook can implement `PaymentSourceAdapter` and yield `IncomingPayment` records.

## Tenant workbook

The project folder contains `tenants.xlsx`, which is the editable tenant registry for the dashboard. Add one tenant per row on the `Tenants` sheet. The required columns are `name`, `unit_number`, `phone`, and `monthly_rent`; the sheet also includes email, UPI/VPA, maintenance, deposit, opening balance, due day, active status, and expected payment identifiers. Use `unit_number` to update an existing tenant. Keep former tenants in the sheet and set `is_active` to `false` so ledger and payment history remains associated with the unit.

Save the workbook after editing. The dashboard checks it whenever it refreshes and also refreshes automatically every 15 seconds. The existing Add tenant form writes to the workbook, then syncs the database used for billing and payment matching. The `Instructions` worksheet in the workbook lists the same editing steps and field meanings.

Command-line reconciliation, billing, reminders, and scheduled monthly runs also sync from the workbook before processing. The CSV tenant import remains available and writes its imported registry back to the workbook.

## Accounting behavior

Payments match exact tenant VPAs and configured identifiers first, followed by conservative fuzzy sender/remarks matching. Reference IDs are unique and duplicate imports are skipped. Unmatched transactions remain recorded for later manual review. Matched payments reduce the latest open monthly ledger; excess becomes tenant credit. A new month carries forward only the latest ledger's net due, preventing arrears from being counted again in older ledger snapshots. Opening balances are positive for arrears and negative for an opening advance; positive opening advances are initialized by importing them into `credit_balance`.

The rent reminder cycle runs at 09:00 on the 2nd in the configured timezone. Reminders are marked sent after the adapter succeeds; local default delivery only logs messages. Twilio transport failures raise errors and are not marked sent. Production deployment should additionally configure backups, restricted access, encrypted secrets, monitoring, and a secure source adapter for authenticated bank data.

## Rental agreements

Create agreements in the dashboard for active tenants. The agreement snapshots tenant details from `tenants.xlsx` and records the property address, check-in date, rental start and end, monthly rent, security deposit, rent due day, additional terms, and names/contact details of other residents. Both the tenant and landlord receive separate signing links; copy each link and share it only with the named signer. If a pending link is lost, create a fresh link from the agreement list; the previous link for that party is revoked. After both parties sign, the completed PDF is available in the dashboard and through either signer's link.

The signer page records a typed name, a drawn signature image, consent, and the server timestamp. It is not Aadhaar eSign or a Digital Signature Certificate and does not independently verify identity. Signing links are bearer credentials; the app stores token hashes, and links keep the secret in the URL fragment. The generated form is a record of the supplied terms, not legal advice or proof that stamp duty or registration has been completed. Check applicable Kerala stamp duty, execution, and registration requirements for the specific lease before signing. The app does not email links automatically.

The main tenant's agreement link also lets them upload one government ID proof per listed occupant, including the main tenant. The uploader confirms they are authorized to submit each person's proof; that confirmation and the main tenant's name are recorded with the upload. Acceptable formats are PDF, JPEG, and PNG, up to 8 MB each; a PDF can contain both sides. The app does not read ID numbers and does not retain original filenames. ID files are Fernet-encrypted in the database and managers can download them from the unit's agreement row. A replacement upload overwrites that occupant's previous file. The encryption key lives in `.env`; generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, then keep it in `.env` or your deployment's secret manager. Use the same key on every app instance. Losing or changing the key makes stored proofs unreadable. Do not commit `.env` or include it in ordinary project backups; back up the key separately and securely.

## Tenant onboarding portal

From the admin dashboard's **Tenant onboarding** panel, choose an active unit and create a private tenant link. Creating another link for that unit revokes the previous one. The tenant opens `/tenant`, enters their name and contact details, adds each other occupant with contact details, and saves the profile for manager review. Tenants cannot change their unit, rent, deposit, or billing fields. The admin dashboard lists submitted occupants and separate ID-proof status/download links for each person. Approving a submission updates the main tenant's name, phone, email, and UPI/VPA in the database and `tenants.xlsx`; additional occupant details remain attached to that reviewed submission. A later tenant update creates a new pending submission.

The tenant link is a bearer credential stored as a hash in the database and placed in the URL fragment, so it is not sent in ordinary HTTP requests. Keep the link private and create a replacement if it is shared with the wrong person. Each ID proof is a separate PDF/JPEG/PNG file up to 8 MB and is encrypted in the database using the same `ID_PROOF_ENCRYPTION_KEY` described above. Only managers authenticated to the dashboard can list submissions and download these files. Remote tenant access requires the public HTTPS deployment settings described above; with the default local-only setup, the tenant portal is reachable only from the same computer.

## Render deployment

`render.yaml` defines the FastAPI web service for Render Free. Create a Neon PostgreSQL database and enter its connection URL as `DATABASE_URL` when Render prompts for Blueprint secrets; the app includes the Psycopg 3 SQLAlchemy driver. Render's free web filesystem is temporary, so set `TENANT_REGISTRY_MODE=database`. Tenant records and encrypted ID proofs then remain in PostgreSQL. The dashboard can download the current `tenants.xlsx`; after editing it, upload the workbook in the dashboard to apply changes to the database. The generated workbook itself is not durable on Render.

The Render Blueprint derives `PUBLIC_APP_URL` from Render's assigned HTTPS URL. Enter manager dashboard credentials, the existing `ID_PROOF_ENCRYPTION_KEY`, and KSEB credentials in Render's secret prompts; do not commit `.env` or workbook/database files. Keep an independent secure backup of the database and encryption key. Render Free services can sleep after 15 minutes without traffic, and the free plan does not provide a reliable always-running monthly scheduler; use the dashboard's KSEB fetch action or set up a separate scheduled runner for monthly retrieval.

## KSEB solar and electricity bills

The dashboard can import a monthly KSEB WSS PDF from **KSEB solar and electricity → Import downloaded bill PDF**, or choose **Fetch all KSEB bills**. The fetcher signs in once, discovers the services linked to the WSS account from the authenticated dashboard, and queries Billing History for the latest eligible bill in the past 12 months for each service. Optional `KSEB_CONSUMER_NUMBERS` can list services explicitly if the portal does not expose its linked-service list. Individual monthly readings and bill amounts are stored per consumer number. The dashboard shows combined property totals and a separate selected-month table for every consumer number, including imported, exported, solar-generated, solar self-used, site-consumed units and bill payable. Expand a row's reading bands to see normal, off-peak and peak usage.

The project-root `solar_consumption.xlsx` workbook contains a **Solar Usage** sheet with each consumer/month, meter-band readings, derived usage, bill amount, source and fetch time, plus a **Monthly Summary** sheet. It is rebuilt from saved per-consumer bill records after every KSEB fetch or PDF import. The dashboard's **Download solar workbook** link refreshes it from the database before downloading. Excel recalculates workbook formulas when opened. Services without solar readings are recorded with zero solar generation. The app does not recalculate tariff charges or distribute the group bill to tenants.

For local WSS retrieval, run `Copy-Item .env.example .env`, then enter the WSS login ID and password in `.env`. The linked services are discovered from the authenticated account dashboard; set `KSEB_CONSUMER_NUMBERS` only if you need to provide a comma-separated list explicitly. Install dependencies with `pip install -r requirements.txt`, then install the browser once with `playwright install chromium`. Never put credentials or OTPs in chat or commit `.env`. The KSEB scheduler runs once monthly at 09:00 on `KSEB_FETCH_DAY` (default 15) when `python -m rental_automation --scheduler` is running. The dashboard's **Fetch all KSEB bills** button or `python -m rental_automation --fetch-kseb` runs retrieval immediately. Restart the dashboard after editing `.env`. The CLI can import a downloaded PDF with `python -m rental_automation --import-kseb-bill .\bill.pdf`.

On Windows, after setting up `.env` and Chromium, run `powershell -ExecutionPolicy Bypass -File .\scripts\register-kseb-monthly-task.ps1` once to register a user-level monthly task. It runs `python -m rental_automation --fetch-kseb` on the configured `KSEB_FETCH_DAY` at 09:00 in the current Windows account's interactive session and starts when available after sleep. The account must be signed in when the task runs. Use `-Unregister` to remove it. Use either this Windows task for KSEB or the app's `--scheduler` job so the same bill is not fetched twice.

KSEB WSS requires a JavaScript-enabled sign-in and has a Billing History screen. This integration uses a local browser session, stops if KSEB requests an OTP/CAPTCHA, and offers PDF import as a fallback if the portal changes its download flow. It does not save a browser profile or log credentials. The displayed data is the generator/property total; beneficiary-to-tenant allocation remains unconfigured. See the [KSEB WSS portal](https://wss.kseb.in/) and its [Billing History guide](https://wss.kseb.in/selfservices/billHisGuide).
