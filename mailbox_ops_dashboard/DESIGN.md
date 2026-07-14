# Mailbox Operations Dashboard — Design

A **local, offline** operational dashboard that turns an operational mailbox
(NIC Mail Cloud / UIDAI-style intranet mailbox) into **tickets** reconstructed
from email threads, tracks their history and pending action, detects the
responsible agent where possible, and gives managers/supervisors expandable
dashboards.

> **Non-negotiables:** local only · no external AI API · no cloud storage ·
> no email data leaves the machine · secrets in `.env` · hashed local passwords.

---

## 1. Architecture

```
                     ┌──────────────────────────────────────────────┐
   INGESTION         │              CONNECTORS (pluggable)           │
                     │  gmail | imap(NIC stub) | eml-export | manual │
                     └───────────────────────┬──────────────────────┘
                                              │ raw messages
                                              ▼
                     ┌──────────────────────────────────────────────┐
   NORMALIZE         │            email_normalizer.py                │
                     │  → single NormalizedEmail dict (schema below) │
                     └───────────────────────┬──────────────────────┘
                                              │ dedup (internet_message_id)
                                              ▼
                     ┌──────────────────────────────────────────────┐
   PERSIST           │   db/models.py  (SQLAlchemy, SQLite→Postgres) │
                     │   emails · tickets · ticket_events · users …  │
                     └───────────────────────┬──────────────────────┘
                                              ▼
   RECONSTRUCT       thread_mapper → ticket_builder → status_engine
                                              │        sla_engine
                                              │        agent_detection_engine
                                              ▼
                     ┌──────────────────────────────────────────────┐
   PRESENT           │   dashboard/app.py (Streamlit) + RBAC + audit │
                     └──────────────────────────────────────────────┘
```

- **Connectors** are provider-agnostic and all emit the *same* normalized
  object, so the rest of the pipeline never knows or cares where an email
  came from. Adding NIC IMAP later = implement one class.
- **Ingestion mode** (`live_sync` / `export` / `manual`) is recorded on every
  email so the Data-Quality dashboard can report the active source.
- **Pipeline is idempotent**: re-ingesting the same email is a no-op
  (dedup on `internet_message_id`), so `recalculate-tickets` can be re-run
  safely.
- **Engines are pure-ish functions** over persisted data — they can be
  re-run at any time to rebuild ticket state without re-ingesting.

### Layering / migration path
| MVP | Production target |
|-----|-------------------|
| SQLite | PostgreSQL (SQLAlchemy — swap `DATABASE_URL`) |
| Streamlit dashboard | Streamlit on internal VM, or FastAPI + React |
| Local pbkdf2 password hashing | LDAP / AD / SSO (auth layer isolated) |
| eml/mbox export ingest | NIC IMAP/POP/API from UIDAI VPN |
| Local filesystem raw store | same (raw store stays local by mandate) |

---

## 2. Data Model (core tables)

- **users** — local accounts: `id, email, name, role, internal(bool),
  password_hash, is_active, last_login_at`. `role ∈ {admin, manager,
  senior_viewer, analyst, auditor}`.
- **emails** — one row per normalized message (full NormalizedEmail schema),
  unique on `internet_message_id`. Links to a ticket via `ticket_id`.
- **tickets** — reconstructed operational ticket (full ticket schema §5).
- **ticket_events** — one row per email or manual action attached to a
  ticket, forming the timeline (includes detected-agent columns).
- **ticket_agents** — contributing-agent association
  (`ticket_id, user_id/detected_email, role, action_count, last_action_at`).
- **internal_notes** — supervisor/analyst notes on a ticket.
- **audit_logs** — `timestamp, user_id, action_type, entity_type, entity_id,
  old_value, new_value` for every manual mutation.
- **sla_rules** — per priority/category due-hours + at-risk threshold.
- **sync_runs** — per-ingestion-run stats for the Data-Quality dashboard.

Full column lists live in `db/models.py`. SQLite for MVP; the models use
portable types so `DATABASE_URL=postgresql://…` is the only change needed.

---

## 3. Ingestion Strategy (three modes)

**MODE 1 — Live sync (`live_sync`)**
- `gmail_connector.py`: Gmail API OAuth, **dev/test only**. Reads inbox+sent,
  maps Gmail `threadId` → `provider_thread_id`.
- `imap_connector.py`: provider-agnostic IMAP connector — the **NIC Mail
  Cloud** path. Ships as a working-shaped **stub** (config + fetch loop +
  normalization wired) because live NIC access needs UIDAI VPN + approvals.
  Usable once IMAP/POP is enabled on the intranet.
- `mailbox_sync_service.py` orchestrates: connect → fetch since watermark →
  normalize → dedup → persist → recalc tickets → write a `sync_run` row.

**MODE 2 — Export parser (`export`) — PRIORITY**
- `export_eml_connector.py` + `export_parser.py`: point at a folder, parse
  every `.eml` with Python's stdlib `email` parser, normalize, ingest.
- `.mbox` handled via stdlib `mailbox` module; **PST** left as a documented
  placeholder (needs `libpst`/`readpst` externally — out of MVP scope).
- This mode makes the tool useful **even with zero mailbox login**, which is
  why it is built first.

**MODE 3 — Manual (`manual`)**
- `manual_upload_connector.py`: paste raw headers+body, or upload a single
  `.eml`, from the dashboard. Same normalizer, `folder=manual`.

All three converge on `email_normalizer.normalize()` → identical dict.

### Normalized email object (contract)
`provider, mailbox_id, provider_message_id, provider_thread_id, thread_key,
internet_message_id, in_reply_to, references, subject, normalized_subject,
from_email, from_name, sender_email, sender_name, reply_to, to_emails,
cc_emails, bcc_emails, sent_at, received_at, folder, direction, body_text,
body_snippet, has_attachments, attachment_metadata, raw_headers,
ingestion_mode, ingestion_timestamp`.

---

## 4. Ticket Reconstruction Strategy

`thread_mapper.resolve_thread_key()` assigns each email to a thread using the
first signal that matches, in priority order:

1. **RFC references** — `In-Reply-To` / `References` / `Message-ID` chain
   (union-find over message-id graph → stable `thread_key`).
2. **Provider thread id** (`provider_thread_id`) when the connector supplies it.
3. **Normalized subject** — strip `Re:/Fwd:/[tags]`, lowercase, collapse ws.
4. **Requester email** (same normalized subject + same external party).
5. **Recipient mailbox** (the operational mailbox the thread belongs to).
6. **Date/time window fallback** — same subject within N hours.

`ticket_builder.py` then groups emails by `thread_key` → one **ticket**,
derives requester (first inbound external party), timestamps, and creates one
**ticket_event** per email. Re-runnable and idempotent.

---

## 5. Agent / Executive Detection Strategy

`agent_detection_engine.detect()` runs, per outbound/internal event, an
**ordered signal cascade** and emits `(email, name, source, confidence)`:

| # | Signal | source | confidence |
|---|--------|--------|-----------|
| 1 | Known-users table match (From/Sender/Reply-To in `users`) | `from_header`/… | **high** |
| 2 | `Sender:` header ≠ common mailbox | `sender_header` | high |
| 3 | `Reply-To:` internal, non-common | `reply_to` | medium |
| 4 | `From:` internal, non-common | `from_header` | medium |
| 5 | Signature block parse (regex, only if confident) | `signature` | low |
| 6 | Sent from the common mailbox only | `sent_folder_common_mailbox` | low |
| 7 | none of the above | `unknown` | **unknown** |
| — | Supervisor correction (always wins) | `manual_override` | high |

**Key limitation (called out in the spec):** if every outbound reply leaves a
single shared NIC mailbox and NIC does not expose the delegated sender, we can
only prove *an outbound reply happened*, not *who sent it* → we record
**"Unknown internal agent"** and expose a supervisor correction control.
Multiple agents per thread are tracked in `ticket_agents` (primary owner +
contributing + last-action-by + per-agent action counts + timeline).

---

## 6. Status Inference (rule-based, separate from manual)

`status_engine.infer()` (never overwrites `manual_status`):
- latest email **from requester** → **Pending with Internal Team**
- latest email **from internal/common mailbox** → **Pending with Requester**
- no owner → **Unassigned**
- no update for `STALE_DAYS` → **Stale**
- requester replies after a resolved/closed state → **Reopened**
- urgent / follow-up / escalation keywords → **Escalated** flag
- closure words (resolved/closed/completed) → **Resolved (suggested)**

Dashboard shows `inferred_status` vs `manual_status` and a **mismatch warning**
when they conflict.

`sla_engine.py`: `sla_due_at` from `sla_rules` by priority; status
met / at-risk / breached; ageing-day buckets 0-1, 2-3, 4-7, 8-15, 15+.

---

## 7. Ticket Drill-Down Design (mandatory)

Clicking a ticket opens a detail page with three expandable sections:
1. **Summary** — all ticket fields, inferred-vs-manual, flags.
2. **Timeline** — every inbound/outbound email + internal notes + status/owner
   changes, each outbound annotated with detected agent + source + confidence;
   expand/collapse body, attachment metadata, To/CC.
3. **Action summary** — first/last response time, customer follow-up count,
   agent reply count, agents involved, current pending party, suggested next
   action.
Editable (per role): owner, contributing agent, department, category,
priority, manual status, notes, closure note, detected-agent correction.

---

## 8. Dashboard Design

Streamlit multipage app, gated by login + RBAC:
- **A. Executive Overview** — headline counters (open, new today, unassigned,
  SLA breached/at-risk, ageing >3/>7, escalated, pending internal/requester,
  multi-agent, unknown-agent).
- **B. Work Queue** — filterable/sortable table of all tickets (all spec
  columns) with the full filter set.
- **C. Ticket Drill-Down** — §7.
- **D. Ageing & SLA** — buckets, met/at-risk/breached, avg first-response &
  closure, oldest pending.
- **E. Owner / Team Performance** — per-owner open counts, touched-by,
  last-action-by, breaches, avg ageing, closed-this-week, stale, multi-agent.
- **F. Category / Request Type** — by category, category SLA/ageing, top
  requester domains.
- **G. Mailbox Sync & Data Quality** — last sync, processed/failed/dup today,
  unmapped threads, ownerless tickets, unclear status, outbound w/ unknown
  agent, current ingestion mode.

---

## 9. RBAC & Audit

Roles: **admin, manager, senior_viewer, analyst, auditor**. Permissions are
declared in `auth/roles.py` and enforced by `require(user, permission)` in the
**service layer** (not just hidden UI), so an analyst cannot mutate via any
path. Every manual mutation writes an `audit_logs` row (old→new). Sessions
carry an idle timeout (`SESSION_TIMEOUT_MINUTES`).

---

## 10. Implementation Phases

1. Local DB + normalized email schema  ✅
2. `.eml` export ingestion (priority)   ✅
3. Ticket / thread reconstruction        ✅
4. Ticket event timeline                 ✅
5. Agent / executive detection           ✅
6. Expandable ticket drill-down          ✅
7. Manager / senior dashboards           ✅
8. Role-based access                     ✅
9. Gmail test connector (optional)       ✅ (dev-only)
10. NIC / IMAP connector stub (future)   ✅ (stub)

---

## 11. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| NIC hides delegated sender behind one shared mailbox | Can't attribute agent | "Unknown internal agent" + manual correction; track that *a* reply happened |
| Live NIC access needs VPN + approval | No live sync at first | Export mode built first; tool useful offline |
| Thread reconstruction imperfect (missing headers in exports) | Split/merged tickets | Multi-signal cascade + subject/requester fallback + manual merge path |
| Signature parsing false positives | Wrong agent attributed | Signature signal capped at **low** confidence, never overrides known-users |
| Email PII on disk | Data exposure | Local-only, raw store optional/configurable, `.env` secrets, hashed pw |
| SQLite concurrency at scale | Lock contention | Documented Postgres migration path (one env var) |
| PST parsing needs native libs | Missing format | Documented placeholder, not silently broken |

---

## 12. Future Roadmap

LDAP/AD/SSO auth · NIC IMAP/POP/API live sync from VPN · FastAPI+React UI ·
Postgres · mbox/PST importers · attachment text indexing · configurable
SLA per category/department · email-send-back from dashboard · notification
digests · per-team routing rules.

---

## 13. AUA/KUA/Sub-AUA/Sub-KUA Onboarding Extension

UIDAI's AUA/KUA/Sub-AUA/Sub-KUA onboarding process (application → agreement →
in-principle approval → security/compliance audit → pre-production testing →
production go-live → recurring annual audits) spans months per applicant
across multiple, otherwise-unrelated-looking email threads. A single email
thread is not the right unit of work for it — this section extends the core
ticket model rather than replacing it, converged through discussion:

- No new "Case" table with its own workflow-state machine (rejected as
  over-engineering for this MVP).
- Instead: entity-identity as the join key across tickets, a lightweight
  stage detector reusing the existing rule-based detection pattern, and a
  fixed one-ticket-per-entity rule scoped specifically to onboarding.

### 13.1 Requesting entity

A new `requesting_entities` table: `name`, `entity_type`
(aua/kua/sub_aua/sub_kua/other), `cin`, `pan`, `tan`, `gstin`,
`registration_number`, `parent_entity_id` (self-referential — the sponsoring
parent for a Sub-AUA/Sub-KUA), `known_domains`, `primary_contact_email`,
`auto_created` (flag for entities discovered from content vs. registered
manually), `created_at`.

`tickets.requesting_entity_id` and `emails.requesting_entity_id` link into
it. The ticket drill-down shows "N other tickets for this entity" as a plain
join/panel — no hierarchy, no workflow engine.

### 13.2 Entity detection cascade (`tickets/entity_detection_engine.py`)

Mirrors the existing agent-detection cascade rather than inventing a new
pattern:
1. Known entity by requester email / domain (`known_domains`,
   `primary_contact_email`) → high confidence.
2. Regex extraction of a CIN/PAN/TAN/GSTIN-format identifier from the
   subject/body, matched against existing entities' stored IDs → medium
   confidence. If no entity has that ID yet and the email is
   onboarding-classified (§13.3), a minimal entity record is auto-created
   (`auto_created=True`) so the system is self-bootstrapping rather than
   requiring pre-registration of every applicant.
3. No match → ticket stays unlinked; a supervisor links it manually from the
   drill-down page (mirrors the "Unknown internal agent" correction path).

Runs for **every** email, not just onboarding ones, so cross-category linking
("this entity also has 2 Annual Audit tickets") works regardless of category.

### 13.3 One ticket per entity for onboarding (`thread_mapper.py`)

`is_onboarding_email()` is a keyword classifier (AUA, KUA, Sub-AUA, Sub-KUA,
"authentication user agency", "in-principle approval", "pre-production",
"go-live", etc.) applied at the email level, independent of any existing
ticket.

This is the one genuinely new piece of core reconstruction logic: after the
existing Message-ID/subject/requester/window cascade assigns each email's
`thread_key` (unchanged, still used for every non-onboarding ticket), a new
folding pass runs for onboarding-classified emails whose entity resolves —
it **overwrites** `thread_key` to the entity's single open onboarding
ticket's key (or mints a new canonical key `th_onboarding_<entity_id>` if
none exists yet), regardless of subject-line differences. This is what makes
"one entity, one onboarding ticket" hold even when the application email,
the in-principle-approval email, and the audit-submission email arrive weeks
apart with completely different subjects.

`category` is auto-set to `"AUA/KUA Onboarding"` only when a new ticket is
created this way — never overwritten on rebuild, so a supervisor's manual
reclassification always sticks (same non-destructive-rebuild rule already
used for owner/status/notes).

### 13.4 Stage detection (`tickets/stage_detection_engine.py`)

Structurally identical to `status_engine.py`: an ordered keyword cascade over
all of a ticket's email text, producing `inferred_onboarding_stage` from a
fixed stage list (Application Submitted → Agreement & In-Principle Approval →
Audit / Compliance Certification → Pre-Production Testing → Production
Go-Live), shown as a visual stepper. `manual_onboarding_stage` stays a
separate, human-settable field (mirrors `manual_status`/`inferred_status`),
with a mismatch flag when they disagree. Same honesty caveat as agent
detection: keyword matching on real UIDAI correspondence needs tuning against
actual samples and is not trusted silently — always correctable.

Reaching the final stage does **not** auto-close the ticket. Closing is
always a manual `set_manual_status` action — a heuristic keyword match is not
sufficient grounds to change ticket state on its own.

### 13.5 Document reference tagging, not a DMS

`ticket_events.document_type` (application_form / agreement /
in_principle_letter / audit_report / other) is auto-tagged by the same
keyword cascade used for stage detection, or set manually. It is a pointer
back to the email that already carries the content/attachment — no separate
storage, no versioning. The drill-down shows "Audit report → \[jump to the
7 Mar email from X\]" instead of a document list.

### 13.6 Category taxonomy

`category` remains the existing free-text field for the general mailbox (so
"Address Update", "Pension Linkage" etc. are untouched), but the moment a
ticket is entity-linked, the dashboard's category control becomes a fixed,
enforced dropdown: **AUA/KUA Onboarding**, **Annual Audit**, **Other
(AUA/KUA)** — preventing the category dashboard from fragmenting under typos
or near-duplicate labels for this workflow, without constraining the
unrelated grievance-ticket categories that were never part of this ask.

### 13.7 Re-application handling

If an entity's onboarding is rejected/withdrawn and they re-apply later, a
**new** ticket is always created (never reopened) — `related_previous_ticket_id`
links it back to the earlier attempt so a supervisor pulling up attempt #2
can jump straight to attempt #1's full history via the entity panel.

### 13.8 Explicitly not changed

Annual Audit and Other tickets remain ordinary, independent tickets — one
per thread/cycle as today, closing normally, linked to the entity only via
the FK for cross-reference. No perpetual-reopen state machine, no case
object spanning tickets, no document storage/versioning.
