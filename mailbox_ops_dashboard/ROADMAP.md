# Roadmap

Not committed to — a working list of candidate features, evaluated but
deliberately not built yet, so decisions don't get lost between sessions.
See `DESIGN.md` §12 for the original one-line roadmap summary; this doc
supersedes it with rationale and rough sizing.

Nothing here should be started without an explicit go-ahead.

---

## Done — Zammad-inspired features

Evaluated Zammad (self-hosted open-source helpdesk) as a possible
alternative, then built the parts that fit this project's constraints
(local, audit-logged, no reply-sending from the dashboard) directly in:
**tags**, **saved Work Queue views**, **automation rules** (fire once, at
ticket creation only), **macros / bulk actions**, and **ticket merge &
split**. See README "Zammad-inspired features" for details and known
limitations.

---

## Out of scope (Zammad features evaluated, won't build)

Filtered out before ever being offered as build candidates — none of these
fit a confidential internal operational mailbox with no external-facing
surface:

- **Live chat** — there is no customer-facing channel in this project at
  all; email in, email out (from the mailbox client, never this dashboard).
- **Social media channels** (Twitter/Facebook/etc. ingestion) — same reason;
  out of scope by mandate, not just unbuilt.
- **Customer self-service portal** — would mean external (non-UIDAI-staff)
  users authenticating into this system. Directly conflicts with "no email
  data leaves the machine" / internal-only access model.
- **Knowledge base / FAQ** — a public or semi-public article system implies
  an external or wide internal audience; not a fit for a small ops team's
  internal ticket tool. Could be revisited as a purely internal, admin-only
  "runbook notes" feature if ever wanted, but that's a different feature
  from Zammad's KB.
- **Multi-brand support** — this dashboard runs one operational mailbox for
  one organization; nothing to brand-separate.

## Zammad features — not yet discussed, now scoped below

Time tracking, agent collision detection, and ticket watchers are added to
Near-term below — they fit the internal, audit-logged, multi-agent model
already in place. Multi-language UI is added to Long-term — it fits in
principle (UIDAI is India-wide) but is a bigger i18n investment than a
day-to-day feature.

---

## Near-term (contained scope, clear day-to-day value)

- ~~**Full-text search**~~ **Done.** Work Queue → "Search subject / requester
  / email body" — plain, portable `ILIKE`, deliberately **not** SQLite FTS5
  (would break the one-env-var Postgres path). See README "Additional
  operational features". An index-backed search (Postgres `tsvector` /
  FTS5-on-SQLite specifically) remains a follow-up if this gets slow at
  scale.
- **SLA-breach / stale-ticket notification digest.** The Ageing & SLA
  dashboard already tracks all of this, but it's pull-only — nobody is told
  when something breaches. A local, internal-only digest (e.g. a daily
  summary to a supervisor's inbox) doesn't conflict with the "no
  customer-facing sending" rule, since it's an ops notification, not a
  reply.
- ~~**Bulk actions on the Work Queue.**~~ **Done.** Work Queue → **Bulk
  actions / Macros** — multi-select tickets, apply a set of field changes at
  once (ad hoc, or via a saved macro preset under Admin → Macros). See
  README "Zammad-inspired features".
- ~~**Editable SLA rules UI.**~~ **Done.** `Admin → SLA rules` is now
  editable (edit existing + add category-specific rules), admin-only
  (`configure_sla`), audit-logged. Delivered alongside the broader
  admin-editable settings feature below.
- ~~**Bulk import of `RequestingEntity` records** (CSV).~~ **Done.**
  Admin → Entities → "Bulk import entities (CSV)" — matches by PAN then
  name, never blanks a field a row left empty. See README.
- ~~**Admin-editable detection keyword dictionaries.**~~ **Partially done.**
  Admin → **Detection keywords** covers the escalation/closure
  (`status_engine.py`) and AUA/KUA onboarding classifier
  (`entity_detection_engine.py`) keyword lists — literal-phrase matching
  only (never user-supplied regex), refreshed once per rebuild. **Not**
  covered: `agent_detection_engine.py`'s signature-extraction pattern
  (structurally different from a flat phrase list) and
  `stage_detection_engine.py`'s per-stage/per-doctype keyword sets (9
  separate categories — more UI surface, lower incremental value, higher
  risk of a correctness regression under time pressure than the two that
  shipped). Remains open as a smaller follow-up.
- ~~**Ticket watchers / CC subscribers.**~~ **Done.** 👁 Watch/Unwatch in the
  drill-down; "Watching only" Work Queue filter.
- ~~**Agent collision detection.**~~ **Done.** A `locked_by`/`locked_at`
  marker touched on drill-down open; warns if a different user's touch is
  <10 minutes old. Advisory only, as scoped — no real-time presence.
- **Basic time tracking.** Let an agent log time spent against a ticket
  (start/stop or manual minutes entry), rolled up per ticket and per owner
  in the Owner/Team Performance dashboard. Internal metric only, no billing
  integration implied.

## Medium-term

- **Category-specific SLA rules for the AUA/KUA workflow** (Onboarding vs.
  Annual Audit vs. Other) — the schema already supports this
  (`sla_rules.category`), just needs seed rows + the editable-SLA-rules UI
  above.
- **Per-team/department routing rules** — auto-assign a primary owner by
  category/department instead of leaving every new ticket unassigned.
- ~~**Saved/bookmarked Work Queue filter presets.**~~ **Done.** Work Queue →
  **Saved views** — personal or shared with everyone. See README
  "Zammad-inspired features".
- **mbox/PST importers** — mbox is a small addition (stdlib `mailbox`
  module, same shape as the existing `.eml` importer); PST remains a
  documented placeholder needing external conversion (`readpst`).
- ~~**Richer attachment metadata**~~ **Done.** Filename/content-type/size
  were already captured by the normalizer; now surfaced — a 📎 count column
  on the Work Queue, per-attachment detail in the drill-down timeline.
  Still no document storage or versioning — stays a metadata pointer, not a
  DMS, per the original constraint.

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
- ~~**Health/heartbeat monitoring for scheduled sync jobs**~~ **Done.** A
  health banner on the Data Quality dashboard (live_sync mode only) plus
  `python main.py check-sync-health` — an exit-code check (0/1/2) to wire
  into your own cron/monitoring alerting. Threshold `sync_max_age_hours`
  (default 24h), admin-editable under Admin → Settings. This project still
  doesn't send the alert itself, consistent with "no sending from the
  dashboard" — it just makes staleness checkable from the outside.
- **Multi-language UI** (e.g. Hindi + English, given UIDAI is India-wide) —
  fits in principle, but is an i18n investment (string extraction,
  translated copy, a language switcher) rather than a contained feature,
  so it sits here rather than Near-term.
