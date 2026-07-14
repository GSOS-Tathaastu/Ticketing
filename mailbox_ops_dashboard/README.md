# 📬 Mailbox Operations Dashboard (Local MVP)

Turn an operational mailbox (NIC Mail Cloud / UIDAI-style intranet mailbox)
into **operational tickets** reconstructed from email threads — with ticket
history, pending-action/status detection, best-effort agent attribution, and
expandable dashboards for managers and supervisors.

> **Runs entirely locally.** No external AI API · no cloud storage · no email
> data leaves the machine · secrets in `.env` · hashed local passwords. Built
> for a laptop first, an internal UIDAI server/VM later.

See **[DESIGN.md](DESIGN.md)** for the full architecture, data model,
ingestion/reconstruction/detection strategies, and phased plan (produced
before implementation, as requested).

---

## Quick start (60 seconds)

```bash
cd mailbox_ops_dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # minimal bootstrap only — see note below

python main.py seed-demo      # init DB + load 5 bundled sample emails
python main.py run-dashboard  # open http://localhost:8501
```

Demo logins created by `seed-demo`:

| Email | Password | Role |
|-------|----------|------|
| `admin@uidai.gov.in` | `admin123` | Admin |
| `agent.rao@uidai.gov.in` | `agent123` | Analyst |

> The demo passwords are for local testing only — change them (or don't use
> `seed-demo`) before any real deployment.

> **Setup note:** `.env` is only the initial bootstrap default now. IMAP
> host/port/user/mailbox, internal domains, common mailboxes, and SLA/ageing
> thresholds are meant to be configured **after first login**, by an admin,
> under **Admin → Settings** and **Admin → SLA rules** — not by hand-editing
> `.env` on every deployment. Changes there take effect immediately (no
> restart) and are audit-logged. The one exception: `IMAP_PASSWORD` always
> stays in `.env` on the host — never stored in the database or shown in the
> UI, even to admins.

---

## Three ingestion modes

| Mode | Connector | When to use | Command |
|------|-----------|-------------|---------|
| **2 · Export** *(priority)* | `export_eml_connector` | Live access blocked / pending approval. Parse a folder of `.eml` (or `.mbox`). **Useful with zero mailbox login.** | `python main.py ingest-eml --path ./data/eml_exports` |
| **3 · Manual** | `manual_upload_connector` | Both live + export blocked. Paste/upload a single email. | `python main.py ingest-manual --file msg.eml` (or the dashboard's *Manual ingest* page) |
| **1 · Live (dev)** | `gmail_connector` | Local dev/testing against a real mailbox. | `python main.py sync-gmail` |
| **1 · Live (prod)** | `imap_connector` | NIC Mail Cloud / intranet, from UIDAI VPN, once IMAP approved. | `python main.py sync-imap` |

All four connectors emit the **same normalized email object**, so ticketing,
detection and dashboards are identical regardless of source. Ingestion is
**idempotent** — re-running skips duplicates (dedup on `internet_message_id`).

---

## CLI commands

```bash
python main.py init-db [--admin-email X --admin-password Y]  # create schema + SLA seed
python main.py ingest-eml --path ./data/eml_exports          # Mode 2 (priority)
python main.py ingest-manual [--file msg.eml]                # Mode 3 (stdin if no --file)
python main.py sync-gmail [--query "newer_than:30d"]         # Mode 1 dev
python main.py test-imap                                     # diagnose NIC/IMAP connectivity (run first, on VPN)
python main.py sync-imap [--since 2026-01-01]                # Mode 1 prod (VPN)
python main.py run-dashboard [--port 8501]                   # Streamlit UI
python main.py create-user --email X --name N --role R       # roles: admin|manager|senior_viewer|analyst|auditor
python main.py recalculate-tickets                           # rebuild tickets/status/SLA (safe to re-run)
python main.py export-report [--out report.csv]              # CSV of all tickets
python main.py seed-demo                                     # init + load bundled sample emails
```

---

## Project layout

```
mailbox_ops_dashboard/
├── connectors/          # provider-agnostic sources → normalized email
│   ├── base_connector.py
│   ├── gmail_connector.py          # Mode 1 dev/test (optional deps)
│   ├── imap_connector.py           # Mode 1 prod — NIC/intranet (VPN)
│   ├── export_eml_connector.py     # Mode 2 — .eml/.mbox (priority)
│   └── manual_upload_connector.py  # Mode 3 — paste/upload
├── ingestion/
│   ├── email_normalizer.py         # the single NormalizedEmail contract
│   ├── mailbox_sync_service.py     # dedup → persist → rebuild → SyncRun
│   └── export_parser.py
├── tickets/
│   ├── thread_mapper.py            # RFC/thread-id/subject/party cascade
│   ├── ticket_builder.py           # emails → tickets + timeline events
│   ├── status_engine.py            # rule-based inferred_status
│   ├── sla_engine.py               # SLA + ageing buckets
│   ├── agent_detection_engine.py   # ordered signal cascade + Unknown-agent
│   ├── entity_detection_engine.py  # AUA/KUA requesting-entity cascade (DESIGN.md §13)
│   └── stage_detection_engine.py   # AUA/KUA onboarding-stage keyword cascade
├── dashboard/
│   ├── app.py                      # Streamlit UI (login, RBAC, dashboards A–H, drill-down)
│   └── service.py                  # read/write service layer (RBAC + audit)
├── db/
│   ├── models.py                   # SQLAlchemy schema (SQLite→Postgres)
│   ├── database.py
│   └── migrations_or_init.py       # bootstrap + SLA seed
├── auth/
│   ├── roles.py                    # RBAC permission matrix
│   ├── users.py                    # auth + require() + audit
│   └── security.py                 # pbkdf2 password hashing (stdlib)
├── config/
│   ├── settings.py                 # .env bootstrap defaults
│   └── runtime_settings.py         # admin-editable DB overrides (never IMAP_PASSWORD)
├── tests/                          # pytest + bundled sample .eml
├── main.py                         # CLI entrypoint
├── desktop_launcher.py             # .exe entry point (server + open browser)
├── mailbox_ops_dashboard.spec      # PyInstaller build config
├── build_exe.bat · build_exe.sh    # build the .exe (Windows) / test build (Linux/macOS)
├── DESIGN.md · README.md · .env.example · requirements.txt
```

---

## Database schema (summary)

SQLite for the MVP; portable SQLAlchemy types give a one-env-var path to
PostgreSQL. Core tables:

- **users** — local accounts + role + internal flag + `pbkdf2` hash
- **emails** — one row per normalized message (the full NormalizedEmail
  contract), unique on `internet_message_id`, FK → ticket
- **tickets** — reconstructed ticket (owner, dept, category, priority,
  manual+inferred status, pending_with, ageing, SLA, escalation/reopen flags,
  closure note, first-response/resolved timestamps)
- **ticket_events** — timeline row per email/manual action, incl. detected
  agent (email/name/source/confidence) and action type
- **ticket_agents** — primary owner + contributing agents (+ per-agent action
  counts and last-action time) → multi-agent support
- **internal_notes** — supervisor/analyst notes (+ closure notes)
- **audit_logs** — every manual mutation (`old_value` → `new_value`)
- **sla_rules** — per-priority/category due-hours + at-risk window, editable
  under Admin → SLA rules
- **app_settings** — admin-editable key/value overrides of non-secret `.env`
  config (IMAP host/port/user/mailbox, domains, thresholds, ingestion mode),
  applied at runtime with no restart. `IMAP_PASSWORD` is never stored here.
- **sync_runs** — per-ingestion stats (Data-Quality dashboard)
- **requesting_entities** — AUA/KUA/Sub-AUA/Sub-KUA onboarding extension
  (DESIGN.md §13): entity identity (CIN/PAN/TAN/GSTIN, type, sponsoring
  parent for Sub-AUA/Sub-KUA), a join key across tickets — not a case/workflow
  object. `tickets`/`emails` gain `requesting_entity_id`;
  `tickets` also gain `inferred_onboarding_stage` / `manual_onboarding_stage`
  / `related_previous_ticket_id`; `ticket_events` gains `document_type`.
- **ticket_tags** — free-form tags on a ticket (see "Zammad-inspired
  features" below)
- **saved_views** — a user's saved Work Queue filter combination, personal or
  shared
- **automation_rules** — condition → action(s), evaluated once at ticket
  creation
- **macros** — a named, reusable bundle of field changes for bulk actions
- `emails` also gains `manual_thread_key` — a manual, permanent override of
  the normal thread-reconstruction, used by ticket merge/split

Full column lists are in [`db/models.py`](db/models.py). **Note:** the MVP
uses `create_all` with no Alembic (see `db/migrations_or_init.py`) — an
existing SQLite file from before these tables were added will pick them up
fine on the next `init-db`/app start (missing tables get created), but if you
want a fully clean slate, delete `data/mailbox_ops.db` and re-run
`init-db`/`seed-demo`.

---

## Dashboards (A–H)

Login-gated, role-aware. Every mutation is permission-checked **at the service
layer** and written to the audit log.

- **A · Executive Overview** — total / open / new today / closed today /
  unassigned / SLA breached / at-risk / ageing >3 & >7 / escalated / pending
  internal & requester / multi-agent / unknown-last-agent.
- **B · Work Queue** — filterable table (all spec columns) + the full filter
  set (owner, contributing agent, dept, status, inferred status, priority,
  ageing bucket, SLA breached, unassigned, escalated, last-email-from,
  unknown agent, multi-agent). Row → opens drill-down.
- **C · Ticket Drill-Down** *(mandatory)* — expandable **summary**,
  **full timeline** (every inbound/outbound email + notes + status/owner
  changes, each outbound annotated with detected agent + source + confidence,
  expandable bodies, attachment metadata, To/Cc), and **action summary**
  (first/last response, follow-ups, replies, agents involved, pending party,
  suggested next action). Editable per role: owner, contributing agent, dept,
  category, priority, manual status, notes, closure note, detected-agent
  correction, **and requester email/name** (a data-quality fix for a garbled
  name or the wrong participant picked out of a multi-recipient thread —
  locked against being overwritten by the next rebuild), with an
  **inferred-vs-manual mismatch warning**.
- **D · Ageing & SLA** — buckets (0-1/2-3/4-7/8-15/15+), met/at-risk/breached,
  avg first-response & closure, oldest pending.
- **E · Owner / Team Performance** — open by owner, touched-by-agent counts,
  last-action-by, breaches, avg ageing, closed-this-week, stale, multi-agent.
- **F · Category / Request Type** — by category, category SLA/ageing, top
  requester domains.
- **G · Mailbox Sync & Data Quality** — last sync, processed/failed/dup today,
  unmapped emails, ownerless tickets, unclear status, outbound replies +
  unknown-agent count, multi-agent ambiguity, **current ingestion mode**, and
  a **"Download full report (.zip)"** button — every dashboard's data (raw
  tickets + executive/ageing/SLA/owner/category/data-quality metrics +
  requesting entities) as one .zip of CSVs, also available via
  `python main.py export-full-report`.
- **H · AUA/KUA Onboarding** *(DESIGN.md §13)* — one row per requesting
  entity with a visual stage stepper (Application Submitted → Agreement &
  In-Principle Approval → Audit/Compliance Certification → Pre-Production
  Testing → Production Go-Live) and a count of the entity's other tickets
  (Annual Audit / Other, kept separate per cycle). The drill-down page shows
  the same entity card, stage stepper, and a document-reference index
  (pointers back to the source email — no document store) whenever a ticket
  is entity-linked. Entities can be corrected under **Admin → Entities**
  (name/type/CIN/PAN/TAN/GSTIN/domains) — useful when auto-detection guesses
  wrong, e.g. picking a sender's personal name instead of the company's.

*(This is a Streamlit MVP: charts are native bar charts + metric tiles. The
drill-down uses `st.expander` sections for the expand/collapse requirement.)*

---

## Zammad-inspired features

Four capabilities borrowed from Zammad (evaluated as an open-source
alternative, then built directly into this dashboard instead) — scoped to
what fits a local, no-external-send, audit-logged internal mailbox tool:

- **Tags** — free-form labels on a ticket (`🏷️ Tags` in the drill-down;
  filterable from the Work Queue), independent of the fixed `category` field.
  Requires `manage_tags` (Admin, Manager, Analyst).
- **Saved views** (Zammad "Overviews") — save the Work Queue's current filter
  combination under a name, personal or shared with everyone. Work Queue →
  **Saved views**.
- **Automation rules** (Zammad "Triggers") — Admin → **Automation rules**: a
  condition (subject/body/requester email/requester domain contains or
  equals a value) plus one or more actions (set category/priority/department,
  assign an owner, add a tag). **Fires exactly once, only when a ticket is
  first created** — never on a later `recalculate-tickets` — so a rule can
  never silently overwrite a field a human edited afterwards. Requires
  `configure_automation` (Admin only) to manage rules.
- **Macros & bulk actions** — Work Queue → **Bulk actions / Macros**: select
  multiple tickets and apply a set of field changes at once, either ad hoc or
  via a saved, reusable **macro** preset (Admin → **Macros**, requires
  `configure_macros`). Applying a macro only needs the permission for
  whichever fields it actually touches — the same audited mutation functions
  the single-ticket edit panel uses, just looped.
- **Ticket merge & split** — drill-down → **Merge / Split** tab (requires
  `merge_split_tickets`, Admin/Manager). Merge combines two tickets that
  turned out to be the same issue (the duplicate's emails, internal notes,
  and — if the survivor has none — owner all move to the survivor; the
  duplicate ticket then disappears). Split peels selected emails off into a
  brand-new ticket when a thread actually covers two unrelated issues. Both
  work by pinning the moved emails to a specific ticket via
  `Email.manual_thread_key`, which permanently overrides the normal
  message-id/subject thread-reconstruction on every future rebuild — see
  `tickets/thread_mapper.py`. **Known limitation:** a genuinely new reply
  that references a pre-merge message via `In-Reply-To` will follow its
  original auto-computed thread, not necessarily the merged destination —
  acceptable for the same reason as the other best-effort detection
  cascades in this project (manual correction is always available).

---

## Role-based access

Enforced in the **backend service layer** (`auth/users.require`), not only the
UI, so an under-privileged user cannot mutate via any code path.

| Role | Capabilities |
|------|--------------|
| **Admin** | Everything: ingestion/mailbox/domain/SLA config, user management, all dashboards & edits, automation rules, macro presets |
| **Manager / Supervisor** | All dashboards & tickets, assign/reassign owners, correct detected agent, edit priority/category/status, notes, tags, merge/split tickets, view audit |
| **Senior Viewer / Leadership** | Read-only dashboards + drill-down, no edits |
| **Analyst** | View team/assigned tickets, update permitted status, add notes, tags |
| **Auditor** | Read-only ticket history + audit logs |

Only **Admin** holds `configure_mailbox` / `configure_internal_domains` /
`configure_sla` / `configure_ingestion` — Managers and Analysts can edit
ticket *data* (owner, status, notes, agent corrections, requester
corrections) but never the system's own operational configuration. This is
enforced at the service layer (`config/runtime_settings.py`'s `OVERRIDABLE`
map declares which permission each setting needs), not just by hiding the
Admin page in the UI.

---

## Agent / executive detection

Ordered signal cascade (see `agent_detection_engine.py`): known-users table →
distinct `Sender` header → internal `Reply-To` → internal `From` → signature
(low confidence) → common-mailbox-only → unknown. Output is
`(email, name, source, confidence∈{high,medium,low,unknown})`. Supervisor
correction always wins (high).

**Central limitation (by design):** if every outbound reply leaves a single
shared NIC mailbox and NIC does not expose the delegated sender, the system can
prove only that *an outbound reply happened* — not *who* sent it. It then shows
**"Unknown internal agent"** and exposes a supervisor correction control in the
drill-down. Multiple agents per thread are tracked (owner + contributors +
last-action-by + per-agent counts + timeline).

---

## Sample `.eml` ingestion test

Five bundled emails in `tests/sample_eml/` form two tickets: a 4-message
address-update thread (requester → common-mailbox ack → requester follow-up →
named-agent resolve) and a single pension grievance. They exercise threading,
direction detection, agent attribution (both "Unknown internal agent" and a
header-detected named agent), escalation keywords, and status inference.

```bash
python -m pytest tests/ -v          # 12 tests: normalizer, detection, ingestion, threading
python main.py seed-demo            # or load them into the dashboard
```

---

## Gmail dev/test setup (Mode 1, optional)

Gmail is **for local development/testing only** — the production target is NIC
Mail Cloud via IMAP.

1. `pip install google-api-python-client google-auth-oauthlib`
2. Google Cloud Console → create OAuth client (Desktop app) → download
   `credentials.json` into `mailbox_ops_dashboard/`.
3. Enable the Gmail API for the project; scope used is read-only
   (`gmail.readonly`).
4. `python main.py sync-gmail` → completes OAuth in the browser, caches
   `token.json` locally. Both files are git-ignored.

---

## NIC Mail Cloud / IMAP deployment assumptions

The production connector (`imap_connector.py`) is provider-agnostic IMAP over
SSL, expected to run **only from inside the UIDAI VPN/intranet**. Assumptions:

- IMAP (or POP) is **enabled and approved** on NIC Mail Cloud for the
  operational mailbox (this typically requires a UIDAI/NIC request).
- Service or delegated credentials are provided in `.env`
  (`IMAP_HOST/PORT/USER/PASSWORD`) — never committed.
- Both **INBOX and Sent** are readable so outbound replies are captured
  (adjust folder names in `imap_connector._folders()` to match the server).
- The host is reachable only on the intranet — it will time out from the public
  internet, which is expected.
- If IMAP/POP/API access is **not** granted, fall back to **Mode 2 (export)**:
  export mailbox folders to `.eml`/`.mbox`/PST from the mail client and ingest
  the folder. (PST needs external conversion: `readpst -e -o out/ file.pst`,
  then `ingest-eml`.)

**Testing against the live mailbox:** this has to happen on a machine that's
actually on the UIDAI VPN/intranet — an IMAP client connection can't reach
NIC Mail Cloud from anywhere else, credentials or not. Once `.env` is filled
in on that machine, run `python main.py test-imap` **first** — it diagnoses
DNS → TCP → TLS → LOGIN → mailbox SELECT → message count step by step and
stops at the first failure, without ingesting anything or printing email
content/credentials. Only once every step passes, run `python main.py
sync-imap` for a real (idempotent, safe-to-repeat) ingest.

---

## Intranet / VPN deployment notes

- Deploy on an internal UIDAI server/VM; bind Streamlit to `127.0.0.1` (default
  in `.streamlit/config.toml`) or an intranet-only interface behind a reverse
  proxy with TLS.
- Telemetry is **disabled** (`gatherUsageStats = false`) so nothing leaves the
  host.
- Keep the SQLite DB and any raw email store on local/intranet disk only. Raw
  storage is **optional** (`STORE_RAW_EMAIL=false` to disable) and stays local.
- For multi-user/production, migrate to PostgreSQL (`DATABASE_URL=postgresql+…`)
  and put the app behind the intranet SSO/reverse proxy.
- Auth is isolated (`auth/`) to keep a clear **LDAP/AD/SSO** integration path.

---

## Building the Windows `.exe`

A standalone, double-click `.exe` for machines without Python installed —
useful for a non-technical operator's laptop, or a coordinator machine
running the one shared instance everyone else reaches over the intranet
(pick either deployment model; the `.exe` itself doesn't force one — see
`MAILBOX_OPS_BIND_HOST` below).

**This has to be built ON Windows.** PyInstaller does not cross-compile —
running the build on Linux/macOS produces a Linux/macOS binary, not a
`.exe`. From a Windows machine with Python 3.11+ installed:

```bat
build_exe.bat
```

This creates a clean build venv, installs dependencies + PyInstaller, and
produces `dist\MailboxOpsDashboard.exe` — a single ~120MB file with the
Python interpreter and every dependency (Streamlit, pandas, SQLAlchemy,
etc.) bundled in. **Nothing needs installing on the target machine** —
no Python, no `pip install`, and therefore no risk of a user installing an
incompatible package version, since they never touch that layer at all.
`build_exe.sh` runs the same spec on Linux/macOS for local testing only
(still not a `.exe`).

**Design choice — no native app window.** The `.exe` starts the dashboard
server and opens it in the user's **default browser**, rather than wrapping
it in a native window (the `pywebview` approach evaluated earlier). That
alternative needs the Microsoft Edge WebView2 runtime — present by default
on current Windows 10/11, but a real gap on an older or locked-down
government image, and not something bundling can fix since it's an OS
component, not a Python package. Opening the default browser needs nothing
beyond what every Windows install already has. The tradeoff: it looks like
a browser tab, not a standalone app window.

**Where data lives.** The SQLite database, raw email store, and `.env` all
live in a `data/` folder **next to the `.exe`**, not inside it — verified
against an actual built binary, not assumed: `config/settings.py` detects
the frozen state and resolves paths from `sys.executable`'s directory
(stable across runs), never from PyInstaller's temp extraction directory
(wiped after every run, which would otherwise silently reset the database
every single launch). Copy `.env.example` next to the `.exe`, rename it to
`.env`, and fill in what's needed before first login — same non-secret
settings as ever (IMAP host/domains/thresholds are also editable post-setup
under Admin → Settings, per the runtime-settings feature above).

**One vs. many people.** `MAILBOX_OPS_BIND_HOST` (unset by default, meaning
`127.0.0.1`) controls this without needing a different build:
- Default (`127.0.0.1`) — this machine only. Right for a single analyst's
  personal standalone copy.
- Set to `0.0.0.0` or the machine's LAN IP — a shared central instance,
  reachable by others over the intranet at `http://<that-ip>:8501`. Right
  for the "one shared instance, everyone else uses a browser" model the
  RBAC/audit design assumes.

**First launch is slower** (a few seconds) — `--onefile` mode re-extracts
the bundle to a temp directory on every run; subsequent page loads are
normal speed. **Unsigned-exe warnings** (Windows SmartScreen / Defender)
are normal for PyInstaller output without a code-signing certificate —
resolve via an internal IT allowlist entry or an org code-signing cert if
available, not by disabling protections.

---

## Security

Local deployment only · no external AI API · no cloud storage · secrets in
`.env` (git-ignored) · credentials never committed · **pbkdf2-hashed**
passwords · session idle timeout (`SESSION_TIMEOUT_MINUTES`) · raw email store
optional & local · backend-enforced RBAC · full audit trail of manual actions.

---

## Known limitations

- **Agent attribution** is best-effort; a single shared NIC mailbox without
  delegated-sender info yields "Unknown internal agent" (manual correction
  provided).
- **Thread reconstruction** relies on RFC headers; exports that strip
  `Message-ID`/`References` fall back to subject+requester+window heuristics and
  may occasionally split or merge threads (a fallback message-id is synthesized
  when absent).
- **Signature parsing** is deliberately conservative (capped at low confidence).
- **PST** import is a documented placeholder (needs external `readpst`).
- **SQLite** suits a single laptop/VM; concurrent multi-user load wants
  PostgreSQL.
- Streamlit MVP UI (functional, not pixel-polished); charts are native.
- No email **sending** from the dashboard by design — agents keep replying from
  the mailbox.
- **AUA/KUA entity/stage detection** (DESIGN.md §13) is a keyword cascade over
  the same public UIDAI process documents, not real correspondence samples —
  tune the patterns in `entity_detection_engine.py`/`stage_detection_engine.py`
  once real onboarding emails are available. Never trusted silently: a
  supervisor can always relink the entity or set the stage manually, and
  reaching the final stage never auto-closes the ticket.

---

## Risk list

| Risk | Mitigation |
|------|-----------|
| NIC hides delegated sender behind one shared mailbox | "Unknown internal agent" + manual correction; track that a reply happened |
| Live NIC access needs VPN + approvals | Export mode built first; tool useful offline |
| Imperfect threading on stripped exports | Multi-signal cascade + manual review |
| Signature false positives | Low-confidence cap; never overrides known-users |
| Email PII on disk | Local-only, optional raw store, `.env` secrets, hashed pw, audit |
| SQLite concurrency at scale | Documented Postgres migration (one env var) |

---

## Future roadmap

LDAP/AD/SSO auth · NIC IMAP/POP/API live sync from VPN · FastAPI + React UI ·
PostgreSQL · mbox/PST importers · attachment text indexing · per-category/dept
SLA rules in-UI · notification digests · per-team routing. (No RAG / no DMS —
out of scope by design.)

See [`ROADMAP.md`](ROADMAP.md) for the full candidate list with rationale and
rough sizing — nothing there is committed to; it's a working list so
decisions don't get lost between sessions.
