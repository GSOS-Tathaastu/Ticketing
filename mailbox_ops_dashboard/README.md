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

cp .env.example .env          # edit domains/mailboxes for your environment

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
├── config/settings.py              # all config from .env
├── tests/                          # pytest + bundled sample .eml
├── main.py                         # CLI entrypoint
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
- **sla_rules** — per-priority/category due-hours + at-risk window
- **sync_runs** — per-ingestion stats (Data-Quality dashboard)
- **requesting_entities** — AUA/KUA/Sub-AUA/Sub-KUA onboarding extension
  (DESIGN.md §13): entity identity (CIN/PAN/TAN/GSTIN, type, sponsoring
  parent for Sub-AUA/Sub-KUA), a join key across tickets — not a case/workflow
  object. `tickets`/`emails` gain `requesting_entity_id`;
  `tickets` also gain `inferred_onboarding_stage` / `manual_onboarding_stage`
  / `related_previous_ticket_id`; `ticket_events` gains `document_type`.

Full column lists are in [`db/models.py`](db/models.py).

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
  correction, with an **inferred-vs-manual mismatch warning**.
- **D · Ageing & SLA** — buckets (0-1/2-3/4-7/8-15/15+), met/at-risk/breached,
  avg first-response & closure, oldest pending.
- **E · Owner / Team Performance** — open by owner, touched-by-agent counts,
  last-action-by, breaches, avg ageing, closed-this-week, stale, multi-agent.
- **F · Category / Request Type** — by category, category SLA/ageing, top
  requester domains.
- **G · Mailbox Sync & Data Quality** — last sync, processed/failed/dup today,
  unmapped emails, ownerless tickets, unclear status, outbound replies +
  unknown-agent count, multi-agent ambiguity, **current ingestion mode**.
- **H · AUA/KUA Onboarding** *(DESIGN.md §13)* — one row per requesting
  entity with a visual stage stepper (Application Submitted → Agreement &
  In-Principle Approval → Audit/Compliance Certification → Pre-Production
  Testing → Production Go-Live) and a count of the entity's other tickets
  (Annual Audit / Other, kept separate per cycle). The drill-down page shows
  the same entity card, stage stepper, and a document-reference index
  (pointers back to the source email — no document store) whenever a ticket
  is entity-linked.

*(This is a Streamlit MVP: charts are native bar charts + metric tiles. The
drill-down uses `st.expander` sections for the expand/collapse requirement.)*

---

## Role-based access

Enforced in the **backend service layer** (`auth/users.require`), not only the
UI, so an under-privileged user cannot mutate via any code path.

| Role | Capabilities |
|------|--------------|
| **Admin** | Everything: ingestion/mailbox/domain/SLA config, user management, all dashboards & edits |
| **Manager / Supervisor** | All dashboards & tickets, assign/reassign owners, correct detected agent, edit priority/category/status, notes, view audit |
| **Senior Viewer / Leadership** | Read-only dashboards + drill-down, no edits |
| **Analyst** | View team/assigned tickets, update permitted status, add notes |
| **Auditor** | Read-only ticket history + audit logs |

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
