# Roadmap

Not committed to — a working list of candidate features, evaluated but
deliberately not built yet, so decisions don't get lost between sessions.
See `DESIGN.md` §12 for the original one-line roadmap summary; this doc
supersedes it with rationale and rough sizing.

Nothing here should be started without an explicit go-ahead.

---

## Near-term (contained scope, clear day-to-day value)

- **Full-text search** over ticket/email content. There's currently no way
  to search subject/body text — only structured filters (owner, status,
  category, etc.) on the Work Queue. SQLite's built-in FTS5 makes this
  cheap. Probably the single most-missed feature once ticket volume grows.
- **SLA-breach / stale-ticket notification digest.** The Ageing & SLA
  dashboard already tracks all of this, but it's pull-only — nobody is told
  when something breaches. A local, internal-only digest (e.g. a daily
  summary to a supervisor's inbox) doesn't conflict with the "no
  customer-facing sending" rule, since it's an ops notification, not a
  reply.
- **Bulk actions on the Work Queue.** Every mutation today is one ticket at
  a time via the drill-down (`dashboard/service.py`'s mutation functions all
  take a single `Ticket`). Multi-select assign-owner / set-status / set
  category from the queue table would matter once a supervisor is managing
  dozens of tickets at once.
- ~~**Editable SLA rules UI.**~~ **Done.** `Admin → SLA rules` is now
  editable (edit existing + add category-specific rules), admin-only
  (`configure_sla`), audit-logged. Delivered alongside the broader
  admin-editable settings feature below.
- **Bulk import of `RequestingEntity` records** (CSV). Entities are
  currently registered one at a time in `Admin → Entities`, or discovered
  automatically from correspondence. If UIDAI already maintains a master
  AUA/KUA registry, importing it up front would make entity linking (and
  the "N other tickets for this entity" view) accurate from day one instead
  of waiting on auto-detection to slowly discover each entity.
- **Admin-editable detection keyword dictionaries.** The status, agent,
  entity, and onboarding-stage detection cascades are all hardcoded regex
  today (`status_engine.py`, `agent_detection_engine.py`,
  `entity_detection_engine.py`, `stage_detection_engine.py`). Surfacing
  these as editable config would let the ops team tune accuracy against
  real UIDAI correspondence without a code change/redeploy — directly
  addresses the "needs tuning against real samples" limitation already
  called out in the README.

## Medium-term

- **Category-specific SLA rules for the AUA/KUA workflow** (Onboarding vs.
  Annual Audit vs. Other) — the schema already supports this
  (`sla_rules.category`), just needs seed rows + the editable-SLA-rules UI
  above.
- **Per-team/department routing rules** — auto-assign a primary owner by
  category/department instead of leaving every new ticket unassigned.
- **Saved/bookmarked Work Queue filter presets** — supervisors likely have
  recurring filter combinations (e.g. "my team's SLA breaches") worth
  saving rather than re-selecting each time.
- **mbox/PST importers** — mbox is a small addition (stdlib `mailbox`
  module, same shape as the existing `.eml` importer); PST remains a
  documented placeholder needing external conversion (`readpst`).
- **Richer attachment metadata** — size and MIME-type surfaced/flagged in
  the UI. Still no document storage or versioning — stays a metadata
  pointer, not a DMS, per the original constraint.

## Long-term / bigger investment

- **LDAP/AD/SSO authentication** — the auth layer (`auth/users.py`,
  `auth/security.py`) is already isolated from the rest of the app
  specifically so this swap doesn't ripple elsewhere.
- **Live NIC IMAP validation from an actual UIDAI VPN-connected machine** —
  `python main.py test-imap` is built and ready; needs to actually be run
  from inside the network once access is approved.
- **PostgreSQL migration** for true multi-user concurrent access — one
  `DATABASE_URL` env var away by design; no schema changes needed.
- **FastAPI + React UI** as a production-grade replacement for the
  Streamlit MVP, if/when the UI needs to go beyond what Streamlit
  comfortably offers (custom auth flows, heavier interactivity, branding).
- ~~**Windows `.exe` packaging**~~ **Done.** PyInstaller, browser-launch
  (not `pywebview` — avoids the WebView2 dependency risk entirely). See
  README "Building the Windows `.exe`". Verified end-to-end against an
  actual built binary (not just the unfrozen script): login, dashboards,
  and persistent data storage next to the exe all confirmed working. Must
  still be built ON Windows — PyInstaller doesn't cross-compile.
- **Health/heartbeat monitoring for scheduled sync jobs** — if
  `sync-imap`/`sync-gmail` runs on a cron in production, nothing currently
  alerts if it starts silently failing; the Data Quality dashboard shows
  "last sync" but someone has to think to check it.
