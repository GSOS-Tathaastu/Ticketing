"""Streamlit dashboard — login, RBAC-gated navigation, dashboards A–G, and the
mandatory expandable ticket drill-down. Run via `python main.py run-dashboard`
or `streamlit run dashboard/app.py`."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth.roles import ROLE_LABELS, has_permission  # noqa: E402
from auth.users import PermissionError_, authenticate  # noqa: E402
from config.settings import settings  # noqa: E402
from db.database import get_session, init_db  # noqa: E402
from dashboard import service as svc  # noqa: E402
from tickets import sla_engine, stage_detection_engine, status_engine  # noqa: E402
from tickets.entity_detection_engine import ENTITY_CATEGORIES  # noqa: E402

st.set_page_config(page_title="Mailbox Operations Dashboard", page_icon="📬", layout="wide")

PRIORITIES = ["low", "normal", "high", "urgent"]
MANUAL_STATUSES = ["", "Open", "In Progress", "Pending", "Resolved", "Closed"]
ENTITY_TYPES = ["aua", "kua", "sub_aua", "sub_kua", "other"]


# --------------------------------------------------------------------------- #
# Session / auth helpers
# --------------------------------------------------------------------------- #
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def current_user():
    return st.session_state.get("user")


def logout():
    for k in ("user", "login_time", "last_active", "active_ticket"):
        st.session_state.pop(k, None)


def enforce_timeout():
    la = st.session_state.get("last_active")
    if la and (_now() - la).total_seconds() > settings.session_timeout_minutes * 60:
        logout()
        st.warning("Session timed out due to inactivity. Please sign in again.")
    st.session_state["last_active"] = _now()


def login_view():
    st.title("📬 Mailbox Operations Dashboard")
    st.caption("Local, offline operational mailbox → tickets. No data leaves this machine.")
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        s = get_session()
        try:
            user = authenticate(s, email, password)
            if user:
                s.commit()
                st.session_state["user"] = {
                    "id": user.id, "email": user.email, "name": user.name,
                    "role": user.role, "department": user.department,
                }
                st.session_state["login_time"] = _now()
                st.session_state["last_active"] = _now()
                st.rerun()
            else:
                st.error("Invalid credentials or inactive account.")
        finally:
            s.close()
    st.info("First run? Initialise the DB and create an admin:\n\n"
            "`python main.py init-db --admin-email you@org --admin-password ****`\n\n"
            "Or load the bundled demo: `python main.py seed-demo`")


class UserObj:
    """Lightweight user object for backend require() calls in the UI layer."""

    def __init__(self, d: dict):
        self.id = d["id"]
        self.email = d["email"]
        self.name = d["name"]
        self.role = d["role"]


def can(permission: str) -> bool:
    u = current_user()
    return bool(u and has_permission(u["role"], permission))


# --------------------------------------------------------------------------- #
# Dashboard A — Executive Overview
# --------------------------------------------------------------------------- #
def page_executive(s):
    st.header("A · Executive Overview")
    m = svc.executive_metrics(s)
    cols = st.columns(5)
    tiles = [
        ("Total tickets", m["total"]), ("Open", m["open"]), ("New today", m["new_today"]),
        ("Closed/resolved today", m["closed_today"]), ("Unassigned", m["unassigned"]),
        ("SLA breached", m["sla_breached"]), ("SLA at risk", m["sla_at_risk"]),
        ("Ageing > 3 days", m["ageing_gt3"]), ("Ageing > 7 days", m["ageing_gt7"]),
        ("Escalated", m["escalated"]), ("Pending internal", m["pending_internal"]),
        ("Pending requester", m["pending_requester"]),
        ("Multi-agent tickets", m["multi_agent"]), ("Unknown last agent", m["unknown_last_agent"]),
    ]
    for i, (label, val) in enumerate(tiles):
        cols[i % 5].metric(label, val)
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Pending distribution")
        st.bar_chart(pd.DataFrame({
            "count": [m["pending_internal"], m["pending_requester"], m["unassigned"]]},
            index=["Pending internal", "Pending requester", "Unassigned"]))
    with c2:
        st.subheader("SLA posture")
        st.bar_chart(pd.DataFrame({
            "count": [m["sla_breached"], m["sla_at_risk"], m["total"] - m["sla_breached"] - m["sla_at_risk"]]},
            index=["Breached", "At risk", "OK/None"]))


# --------------------------------------------------------------------------- #
# Dashboard B — Work Queue
# --------------------------------------------------------------------------- #
def page_work_queue(s):
    st.header("B · Work Queue")
    tickets = svc.all_tickets(s)
    omap = svc.owners_map(s)

    rows = []
    for t in tickets:
        owner = omap.get(t.primary_owner_user_id)
        entity = svc.get_entity(s, t.requesting_entity_id) if t.requesting_entity_id else None
        last_from = "—"
        if t.last_customer_email_at and t.last_agent_email_at:
            last_from = "customer" if svc._aware(t.last_customer_email_at) >= svc._aware(t.last_agent_email_at) else "agent"
        elif t.last_customer_email_at:
            last_from = "customer"
        elif t.last_agent_email_at:
            last_from = "agent"
        rows.append({
            "PK": t.id, "Ticket ID": t.ticket_id, "Subject": (t.subject or "")[:60],
            "Requester": t.requester_email, "Entity": entity.name if entity else "—",
            "Primary owner": owner.name if owner else "—",
            "Contrib.": svc.contributor_count(s, t.id), "Last action by": t.last_action_by_label or "—",
            "Department": t.department or "—", "Category": t.category or "—",
            "Manual status": t.manual_status or "—", "Inferred status": t.inferred_status or "—",
            "Pending with": t.pending_with or "—", "Priority": t.priority,
            "Ageing (d)": t.ageing_days, "Last email from": last_from,
            "Last email": svc._aware(t.last_email_at).strftime("%Y-%m-%d %H:%M") if t.last_email_at else "—",
            "SLA": t.sla_status or "—",
            "Action needed": _action_needed(t),
            "_owner_id": t.primary_owner_user_id, "_escalated": t.is_escalated,
            "_unassigned": t.primary_owner_user_id is None,
            "_multi": svc.contributor_count(s, t.id) > 1,
            "_unknown_agent": (not t.last_action_by_agent_id and bool(t.last_agent_email_at)),
            "_last_from": last_from,
        })
    df = pd.DataFrame(rows)

    with st.expander("Filters", expanded=True):
        c = st.columns(4)
        f_owner = c[0].selectbox("Owner", ["(all)", "Unassigned"] + sorted(
            {r["Primary owner"] for r in rows if r["Primary owner"] != "—"}))
        f_dept = c[1].selectbox("Department", ["(all)"] + sorted({r["Department"] for r in rows}))
        f_status = c[2].selectbox("Manual status", ["(all)"] + sorted({r["Manual status"] for r in rows}))
        f_inferred = c[3].selectbox("Inferred status", ["(all)"] + sorted({r["Inferred status"] for r in rows}))
        c = st.columns(4)
        f_priority = c[0].selectbox("Priority", ["(all)"] + PRIORITIES)
        f_ageing = c[1].selectbox("Ageing bucket", ["(all)"] + sla_engine.AGEING_BUCKETS)
        f_lastfrom = c[2].selectbox("Last email from", ["(all)", "customer", "agent"])
        f_category = c[3].selectbox("Category", ["(all)"] + sorted({r["Category"] for r in rows}))
        c = st.columns(6)
        f_breached = c[0].checkbox("SLA breached")
        f_unassigned = c[1].checkbox("Unassigned")
        f_escalated = c[2].checkbox("Escalated")
        f_unknown = c[3].checkbox("Unknown agent")
        f_multi = c[4].checkbox("Multiple agents")
        f_contrib = c[5].text_input("Contributing agent contains")

    if not df.empty:
        if f_owner == "Unassigned":
            df = df[df["_unassigned"]]
        elif f_owner != "(all)":
            df = df[df["Primary owner"] == f_owner]
        if f_dept != "(all)":
            df = df[df["Department"] == f_dept]
        if f_status != "(all)":
            df = df[df["Manual status"] == f_status]
        if f_inferred != "(all)":
            df = df[df["Inferred status"] == f_inferred]
        if f_priority != "(all)":
            df = df[df["Priority"] == f_priority]
        if f_category != "(all)":
            df = df[df["Category"] == f_category]
        if f_ageing != "(all)":
            df = df[df["Ageing (d)"].apply(lambda d: sla_engine.ageing_bucket(d or 0) == f_ageing)]
        if f_lastfrom != "(all)":
            df = df[df["_last_from"] == f_lastfrom]
        if f_breached:
            df = df[df["SLA"] == sla_engine.BREACHED]
        if f_unassigned:
            df = df[df["_unassigned"]]
        if f_escalated:
            df = df[df["_escalated"]]
        if f_unknown:
            df = df[df["_unknown_agent"]]
        if f_multi:
            df = df[df["_multi"]]
        if f_contrib.strip():
            needle = f_contrib.strip().lower()
            keep = [tid for tid in df["PK"] if any(
                needle in (a.agent_email or "").lower() or needle in (a.agent_name or "").lower()
                for a in svc.agents_for(s, tid))]
            df = df[df["PK"].isin(keep)]

    st.caption(f"{len(df)} ticket(s)")
    display_cols = ["Ticket ID", "Subject", "Requester", "Entity", "Primary owner", "Contrib.",
                    "Last action by", "Department", "Category", "Manual status",
                    "Inferred status", "Pending with", "Priority", "Ageing (d)",
                    "Last email from", "Last email", "SLA", "Action needed"]
    st.dataframe(df[display_cols] if not df.empty else df, use_container_width=True, hide_index=True)

    if not df.empty and can("drilldown_tickets"):
        st.divider()
        options = {f"{r['Ticket ID']} — {r['Subject']}": r["PK"] for _, r in df.iterrows()}
        pick = st.selectbox("Open ticket drill-down", ["(select)"] + list(options))
        if pick != "(select)":
            st.session_state["active_ticket"] = options[pick]
            st.session_state["nav"] = "C · Ticket Drill-Down"
            st.rerun()


def _action_needed(t) -> str:
    if t.primary_owner_user_id is None:
        return "Assign owner"
    if t.sla_status == sla_engine.BREACHED:
        return "SLA breached — act now"
    if t.inferred_status == status_engine.PENDING_INTERNAL:
        return "Reply to requester"
    if t.inferred_status == status_engine.STALE:
        return "Stale — follow up"
    if t.inferred_status == status_engine.REOPENED:
        return "Reopened — review"
    if t.inferred_status == status_engine.PENDING_REQUESTER:
        return "Await requester"
    return "—"


# --------------------------------------------------------------------------- #
# Dashboard C — Ticket Drill-Down (mandatory)
# --------------------------------------------------------------------------- #
def page_drilldown(s):
    st.header("C · Ticket Drill-Down")
    tickets = svc.all_tickets(s)
    if not tickets:
        st.info("No tickets yet. Ingest some email first.")
        return
    labels = {f"{t.ticket_id} — {(t.subject or '')[:60]}": t.id for t in tickets}
    active = st.session_state.get("active_ticket")
    default_label = next((k for k, v in labels.items() if v == active), list(labels)[0])
    chosen = st.selectbox("Ticket", list(labels), index=list(labels).index(default_label))
    ticket = svc.get_ticket(s, labels[chosen])
    st.session_state["active_ticket"] = ticket.id

    omap = svc.owners_map(s)
    agents = svc.agents_for(s, ticket.id)
    events = svc.ticket_events(s, ticket.id)

    # 1 · Summary --------------------------------------------------------
    with st.expander("1 · Ticket summary", expanded=True):
        c = st.columns(3)
        owner = omap.get(ticket.primary_owner_user_id)
        c[0].markdown(f"**Ticket ID:** {ticket.ticket_id}")
        c[0].markdown(f"**Subject:** {ticket.subject}")
        c[0].markdown(f"**Requester:** {ticket.requester_name or ''} <{ticket.requester_email}>")
        c[0].markdown(f"**Primary owner:** {owner.name if owner else '— unassigned —'}")
        contribs = [a for a in agents if a.role == 'contributor']
        c[0].markdown(f"**Contributing agents:** {len(contribs)}")
        c[1].markdown(f"**Manual status:** {ticket.manual_status or '—'}")
        c[1].markdown(f"**Inferred status:** {ticket.inferred_status or '—'}")
        c[1].markdown(f"**Pending with:** {ticket.pending_with or '—'}")
        c[1].markdown(f"**Priority:** {ticket.priority}")
        c[1].markdown(f"**Category / Dept:** {ticket.category or '—'} / {ticket.department or '—'}")
        c[2].markdown(f"**Created:** {_fmt(ticket.created_at)}")
        c[2].markdown(f"**Last email:** {_fmt(ticket.last_email_at)}")
        c[2].markdown(f"**SLA:** {ticket.sla_status or '—'} (due {_fmt(ticket.sla_due_at)})")
        c[2].markdown(f"**Ageing:** {ticket.ageing_days} days")
        flags = []
        if ticket.is_escalated:
            flags.append("🔴 Escalated")
        if ticket.is_reopened:
            flags.append("🔁 Reopened")
        if ticket.manual_override_flag:
            flags.append("✍️ Manual override")
        c[2].markdown("**Flags:** " + (", ".join(flags) if flags else "—"))

        if status_engine.status_mismatch(ticket.inferred_status, ticket.manual_status):
            st.warning(f"⚠️ Status mismatch: inferred **{ticket.inferred_status}** "
                       f"but manual status is **{ticket.manual_status}**.")

    # AUA/KUA onboarding extension (DESIGN.md §13) ------------------------
    if ticket.requesting_entity_id:
        _render_entity_section(s, ticket)

    # 3 · Action summary -------------------------------------------------
    with st.expander("Action summary", expanded=True):
        cust_followups = sum(1 for e in events if e.direction == "inbound") - 1
        agent_replies = sum(1 for e in events if e.direction == "outbound")
        frt = _duration(ticket.created_at, ticket.first_response_at)
        c = st.columns(3)
        c[0].metric("First response time", frt)
        c[0].metric("Customer follow-ups", max(cust_followups, 0))
        c[1].metric("Agent replies", agent_replies)
        c[1].metric("Agents involved", len({a.agent_email or a.agent_name for a in agents}))
        c[2].metric("Current pending party", ticket.pending_with or "—")
        c[2].markdown(f"**Suggested next action:** {_action_needed(ticket)}")

    # Contributing agents list
    with st.expander("Agents on this ticket", expanded=False):
        if agents:
            st.dataframe(pd.DataFrame([{
                "Name": a.agent_name, "Email": a.agent_email, "Role": a.role,
                "Actions": a.action_count, "Source": a.detection_source,
                "Confidence": a.detection_confidence,
                "Last action": _fmt(a.last_action_at),
            } for a in agents]), use_container_width=True, hide_index=True)
        else:
            st.caption("No agent activity detected yet.")

    # 2 · Timeline -------------------------------------------------------
    with st.expander("2 · Full timeline", expanded=True):
        for e in events:
            _render_event(s, e)

    # Edit panel (permission-gated) --------------------------------------
    _edit_panel(s, ticket, omap, events)


def _render_entity_section(s, ticket):
    """AUA/KUA onboarding extension (DESIGN.md §13): entity identity, other
    tickets for this entity, the visual stage stepper, and a document
    reference index — no case object, no document store, just joins and a
    keyword-cascade stepper over the same ticket data."""
    from tickets.entity_detection_engine import ONBOARDING_CATEGORY

    ent = svc.get_entity(s, ticket.requesting_entity_id)
    if ent is None:
        return
    with st.expander("🏢 AUA/KUA Onboarding — Requesting Entity", expanded=True):
        c = st.columns(3)
        c[0].markdown(f"**Entity:** {ent.name}")
        c[0].markdown(f"**Type:** {ent.entity_type or '—'}" + (" (auto-detected)" if ent.auto_created else ""))
        if ent.parent_entity_id:
            parent = svc.get_entity(s, ent.parent_entity_id)
            c[0].markdown(f"**Sponsoring parent:** {parent.name if parent else '—'}")
        c[1].markdown(f"**CIN:** {ent.cin or '—'}")
        c[1].markdown(f"**PAN:** {ent.pan or '—'}")
        c[2].markdown(f"**TAN:** {ent.tan or '—'}")
        c[2].markdown(f"**GSTIN:** {ent.gstin or '—'}")

        others = [t for t in svc.entity_tickets(s, ent.id) if t.id != ticket.id]
        st.markdown(f"**Other tickets for this entity:** {len(others)}")
        if others:
            st.dataframe(pd.DataFrame([{
                "Ticket ID": t.ticket_id, "Category": t.category or "—",
                "Subject": (t.subject or "")[:60], "Status": t.manual_status or t.inferred_status or "—",
                "Created": _fmt(t.created_at),
            } for t in others]), use_container_width=True, hide_index=True)

        if ticket.related_previous_ticket_id:
            prev = svc.get_ticket(s, ticket.related_previous_ticket_id)
            if prev:
                st.info(f"🔗 Related to an earlier attempt: **{prev.ticket_id}** — {prev.subject}")

        if ticket.category == ONBOARDING_CATEGORY:
            st.markdown("**Onboarding stage:**")
            stage = ticket.manual_onboarding_stage or ticket.inferred_onboarding_stage
            idx = stage_detection_engine.STAGES.index(stage) if stage in stage_detection_engine.STAGES else -1
            steps = " → ".join(
                (f"✅ {s_}" if i <= idx else f"⬜ {s_}") for i, s_ in enumerate(stage_detection_engine.STAGES))
            st.markdown(steps)
            st.caption(f"Inferred: {ticket.inferred_onboarding_stage or '—'} · "
                       f"Manual: {ticket.manual_onboarding_stage or '—'}")
            if ticket.onboarding_stage_override_flag:
                st.warning(f"⚠️ Stage mismatch: inferred **{ticket.inferred_onboarding_stage}** "
                           f"but manual stage is **{ticket.manual_onboarding_stage}**.")

        docs = [e for e in svc.ticket_events(s, ticket.id) if e.document_type]
        if docs:
            st.markdown("**Document references** _(pointers to the source email — no separate storage)_")
            st.dataframe(pd.DataFrame([{
                "Document": d.document_type, "Date": _fmt(d.sent_at),
                "From": d.from_name or d.from_email, "Subject": (d.subject or "")[:60],
            } for d in docs]), use_container_width=True, hide_index=True)


def _render_event(s, e):
    icon = {"inbound": "📥", "outbound": "📤"}.get(e.direction, "📝")
    kind = e.event_kind
    if kind == "note":
        st.markdown(f"📝 **Internal note** · {e.from_name} · {_fmt(e.sent_at)}")
        st.info(e.body_text or e.body_snippet or "")
        return
    if kind in ("status_change", "owner_change", "agent_correction"):
        st.markdown(f"🛠️ **{e.subject}** · {e.from_name} · {_fmt(e.sent_at)}  \n{e.body_snippet or ''}")
        return
    header = f"{icon} **{e.direction.upper()}** · {e.from_name or e.from_email} · {_fmt(e.sent_at)}"
    if e.direction == "outbound":
        conf = e.detected_agent_confidence or "unknown"
        who = e.detected_agent_name or e.detected_agent_email or "Unknown internal agent"
        header += f"  \n&nbsp;&nbsp;↳ detected agent: **{who}** _(source: {e.detected_agent_source}, confidence: {conf})_"
    st.markdown(header)
    to_list = ", ".join(svc.load_json(e.to_emails)) or "—"
    cc_list = ", ".join(svc.load_json(e.cc_emails))
    meta = f"To: {to_list}"
    if cc_list:
        meta += f" · Cc: {cc_list}"
    if e.has_attachment:
        atts = svc.load_json(e.attachment_metadata)
        names = ", ".join(a.get("filename") or a.get("content_type", "?") for a in atts)
        meta += f" · 📎 {names}"
    st.caption(meta)
    with st.expander("Show / hide body"):
        st.text(e.body_text or e.body_snippet or "(no body)")
    st.divider()


def _edit_panel(s, ticket, omap, events):
    u = current_user()
    editable = any(can(p) for p in ("assign_owner", "edit_ticket_fields", "set_manual_status",
                                    "add_note", "edit_contributing_agents", "correct_detected_agent",
                                    "manage_onboarding"))
    if not editable:
        st.info("You have read-only access to this ticket.")
        return
    st.subheader("Manage ticket")
    uo = UserObj(u)
    internal = svc.internal_users(s)
    tab_labels = ["Owner & fields", "Status & notes", "Agents"]
    if can("manage_onboarding"):
        tab_labels.append("AUA/KUA Onboarding")
    tabs = st.tabs(tab_labels)

    with tabs[0]:
        if can("assign_owner"):
            opts = {"— unassigned —": None} | {f"{x.name} <{x.email}>": x.id for x in internal}
            cur = next((k for k, v in opts.items() if v == ticket.primary_owner_user_id), "— unassigned —")
            sel = st.selectbox("Primary owner", list(opts), index=list(opts).index(cur))
            if st.button("Save owner"):
                _mutate(s, lambda ss: svc.assign_owner(ss, uo, svc.get_ticket(ss, ticket.id), opts[sel]))
        if can("edit_ticket_fields"):
            c = st.columns(3)
            dept = c[0].text_input("Department", ticket.department or "")
            # Fixed, enforced category list once a ticket is entity-linked
            # (DESIGN.md §13.6) — free text everywhere else.
            if ticket.requesting_entity_id:
                cur_cat = ticket.category if ticket.category in ENTITY_CATEGORIES else ENTITY_CATEGORIES[0]
                cat = c[1].selectbox("Category", ENTITY_CATEGORIES, index=ENTITY_CATEGORIES.index(cur_cat))
            else:
                cat = c[1].text_input("Category", ticket.category or "")
            prio = c[2].selectbox("Priority", PRIORITIES, index=PRIORITIES.index(ticket.priority or "normal"))
            if st.button("Save fields"):
                _mutate(s, lambda ss: svc.set_ticket_fields(ss, uo, svc.get_ticket(ss, ticket.id),
                                                            department=dept, category=cat, priority=prio))

    with tabs[1]:
        if can("set_manual_status"):
            cur = ticket.manual_status or ""
            status = st.selectbox("Manual status", MANUAL_STATUSES,
                                  index=MANUAL_STATUSES.index(cur) if cur in MANUAL_STATUSES else 0)
            closure = ""
            if status in {"Resolved", "Closed"} and can("add_closure_note"):
                closure = st.text_area("Closure note")
            if st.button("Save status"):
                _mutate(s, lambda ss: svc.set_manual_status(ss, uo, svc.get_ticket(ss, ticket.id),
                                                            status or None, closure or None))
        if can("add_note"):
            note = st.text_area("Add internal note")
            if st.button("Add note") and note.strip():
                _mutate(s, lambda ss: svc.add_note(ss, uo, svc.get_ticket(ss, ticket.id), note.strip()))

    with tabs[2]:
        if can("edit_contributing_agents"):
            c = st.columns(2)
            ae = c[0].text_input("Contributing agent email")
            an = c[1].text_input("Name (optional)")
            if st.button("Add contributing agent") and ae.strip():
                _mutate(s, lambda ss: svc.add_contributing_agent(ss, uo, svc.get_ticket(ss, ticket.id),
                                                                ae.strip(), an.strip() or None))
        if can("correct_detected_agent"):
            outbound = [e for e in events if e.direction == "outbound"]
            if outbound:
                emap = {f"{_fmt(e.sent_at)} — {e.detected_agent_name or e.detected_agent_email or 'Unknown'}": e.id
                        for e in outbound}
                pick = st.selectbox("Correct detected agent on outbound event", list(emap))
                c = st.columns(2)
                ce = c[0].text_input("Correct agent email", key="corr_email")
                cn = c[1].text_input("Correct agent name", key="corr_name")
                if st.button("Apply correction"):
                    eid = emap[pick]
                    _mutate(s, lambda ss: svc.correct_detected_agent(
                        ss, uo, svc.get_event(ss, eid), ce.strip() or None, cn.strip() or None))

    if can("manage_onboarding"):
        with tabs[3]:
            entities = svc.list_entities(s)
            eopts = {"— not linked —": None} | {f"{x.name} (id={x.id})": x.id for x in entities}
            ecur = next((k for k, v in eopts.items() if v == ticket.requesting_entity_id), "— not linked —")
            esel = st.selectbox("Requesting entity", list(eopts), index=list(eopts).index(ecur))
            if st.button("Save entity link"):
                _mutate(s, lambda ss: svc.link_ticket_to_entity(
                    ss, uo, svc.get_ticket(ss, ticket.id), eopts[esel]))

            if ticket.requesting_entity_id:
                stage_opts = [""] + stage_detection_engine.STAGES
                cur_stage = ticket.manual_onboarding_stage or ""
                stage_sel = st.selectbox("Manual onboarding stage", stage_opts,
                                         index=stage_opts.index(cur_stage) if cur_stage in stage_opts else 0)
                if st.button("Save onboarding stage"):
                    _mutate(s, lambda ss: svc.set_manual_onboarding_stage(
                        ss, uo, svc.get_ticket(ss, ticket.id), stage_sel or None))

                st.divider()
                st.caption("Re-application (DESIGN.md §13.7): always a new ticket — link it to the prior attempt.")
                same_entity_tickets = [t for t in svc.entity_tickets(s, ticket.requesting_entity_id)
                                       if t.id != ticket.id]
                popts = {"— none —": None} | {f"{t.ticket_id} — {(t.subject or '')[:40]}": t.id
                                              for t in same_entity_tickets}
                pcur = next((k for k, v in popts.items() if v == ticket.related_previous_ticket_id), "— none —")
                psel = st.selectbox("Related previous attempt", list(popts), index=list(popts).index(pcur))
                if st.button("Save related attempt"):
                    _mutate(s, lambda ss: svc.link_previous_attempt(
                        ss, uo, svc.get_ticket(ss, ticket.id), popts[psel]))


def _mutate(_s, fn):
    """Run a mutation in its own committed session, then rerun the app."""
    ss = get_session()
    try:
        fn(ss)
        ss.commit()
        st.success("Saved.")
    except PermissionError_ as exc:
        ss.rollback()
        st.error(f"Not permitted: {exc}")
    except Exception as exc:  # noqa: BLE001
        ss.rollback()
        st.error(f"Error: {exc}")
    finally:
        ss.close()
    st.rerun()


# --------------------------------------------------------------------------- #
# Dashboard D — Ageing & SLA
# --------------------------------------------------------------------------- #
def page_ageing(s):
    st.header("D · Ageing & SLA")
    m = svc.ageing_sla_metrics(s)
    c = st.columns(2)
    c[0].subheader("Ageing buckets")
    c[0].bar_chart(pd.DataFrame({"tickets": list(m["buckets"].values())}, index=list(m["buckets"].keys())))
    c[1].subheader("SLA met / at risk / breached")
    sla = m["sla"]
    c[1].bar_chart(pd.DataFrame({"tickets": [sla.get("met", 0), sla.get("at_risk", 0), sla.get("breached", 0)]},
                                index=["Met", "At risk", "Breached"]))
    c = st.columns(2)
    c[0].metric("Avg first response (hrs)", m["avg_first_response_hours"] if m["avg_first_response_hours"] is not None else "—")
    c[1].metric("Avg closure time (hrs)", m["avg_closure_hours"] if m["avg_closure_hours"] is not None else "—")
    st.subheader(f"Oldest pending tickets (no update ≥ {settings.stale_days}d flagged Stale)")
    if m["oldest"]:
        st.dataframe(pd.DataFrame([{
            "Ticket ID": t.ticket_id, "Subject": (t.subject or "")[:60], "Requester": t.requester_email,
            "Ageing (d)": t.ageing_days, "Inferred status": t.inferred_status, "SLA": t.sla_status,
        } for t in m["oldest"]]), use_container_width=True, hide_index=True)
    else:
        st.caption("No open tickets.")


# --------------------------------------------------------------------------- #
# Dashboard E — Owner / Team Performance
# --------------------------------------------------------------------------- #
def page_owner_perf(s):
    st.header("E · Owner / Team Performance")
    rows = svc.owner_performance(s)
    if rows:
        st.dataframe(pd.DataFrame([{
            "Owner": r["owner"], "Open": r["open"], "SLA breached": r["breached"],
            "Avg ageing (d)": r["avg_ageing"], "Closed this week": r["closed_this_week"],
            "Stale (no update 3d+)": r["stale"], "Multi-agent": r["multi_agent"],
            "Last-action-by count": r["last_action"], "Total": r["count"],
        } for r in rows]), use_container_width=True, hide_index=True)
        st.subheader("Open tickets by owner")
        st.bar_chart(pd.DataFrame({"open": [r["open"] for r in rows]}, index=[r["owner"] for r in rows]))
    st.subheader("Tickets touched by each agent (incl. detected)")
    touches = svc.agent_touch_counts(s)
    if touches:
        st.dataframe(pd.DataFrame(touches), use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# Dashboard F — Category / Request Type
# --------------------------------------------------------------------------- #
def page_category(s):
    st.header("F · Category / Request Type")
    m = svc.category_metrics(s)
    if m["by_category"]:
        df = pd.DataFrame([{
            "Category": c["category"], "Tickets": c["count"], "SLA breached": c["breached"],
            "Avg ageing (d)": c["avg_ageing"],
        } for c in m["by_category"]])
        st.dataframe(df, use_container_width=True, hide_index=True)
        c = st.columns(2)
        c[0].subheader("Tickets by category")
        c[0].bar_chart(df.set_index("Category")["Tickets"])
        c[1].subheader("SLA breach by category")
        c[1].bar_chart(df.set_index("Category")["SLA breached"])
    st.subheader("Top requester domains")
    if m["top_domains"]:
        st.dataframe(pd.DataFrame(m["top_domains"], columns=["Domain", "Tickets"]),
                     use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# Dashboard G — Mailbox Sync & Data Quality
# --------------------------------------------------------------------------- #
def page_data_quality(s):
    st.header("G · Mailbox Sync & Data Quality")
    m = svc.data_quality_metrics(s)
    c = st.columns(4)
    c[0].metric("Current ingestion mode", settings.ingestion_mode)
    c[1].metric("Last sync", _fmt(m["last_sync"]) if m["last_sync"] else "never")
    c[2].metric("Last sync mode", m["last_sync_mode"] or "—")
    c[3].metric("Last sync ok?", "yes" if m["last_sync_success"] else ("no" if m["last_sync_success"] is not None else "—"))
    c = st.columns(4)
    c[0].metric("Processed today", m["processed_today"])
    c[1].metric("Failed today", m["failed_today"])
    c[2].metric("Duplicates skipped today", m["duplicates_today"])
    c[3].metric("Emails without ticket", m["unmapped_emails"])
    c = st.columns(4)
    c[0].metric("Tickets missing owner", m["ownerless_tickets"])
    c[1].metric("Unclear inferred status", m["unclear_status"])
    c[2].metric("Outbound replies detected", m["outbound_replies"])
    c[3].metric("Outbound w/ unknown agent", m["outbound_unknown_agent"])
    st.metric("Multi-agent ambiguity", m["multi_agent_ambiguous"])
    if can("configure_ingestion"):
        st.divider()
        if st.button("🔄 Recalculate tickets now"):
            _mutate(s, lambda ss: svc.recalc(ss, UserObj(current_user())))


# --------------------------------------------------------------------------- #
# Dashboard H — AUA/KUA Onboarding (DESIGN.md §13)
# --------------------------------------------------------------------------- #
def page_onboarding(s):
    st.header("H · AUA/KUA Onboarding")
    st.caption("One ticket per entity for onboarding; Annual Audit / Other tickets stay separate "
              "per cycle, linked only via the requesting entity.")
    rows = svc.onboarding_overview(s)
    if not rows:
        st.info("No entity-linked tickets yet. Entities are detected automatically from onboarding "
               "correspondence, or can be created under Admin → Entities.")
        return
    for r in rows:
        ent, ticket = r["entity"], r["current_onboarding_ticket"]
        with st.container(border=True):
            c = st.columns([2, 3, 1])
            c[0].markdown(f"**{ent.name}**  \n`{ent.entity_type or '—'}`" +
                         (" · auto-detected" if ent.auto_created else ""))
            if ticket:
                idx = (stage_detection_engine.STAGES.index(r["stage"])
                      if r["stage"] in stage_detection_engine.STAGES else -1)
                steps = " → ".join(
                    ("✅" if i <= idx else "⬜") for i in range(len(stage_detection_engine.STAGES)))
                c[1].markdown(f"{steps}  \n{r['stage'] or 'Onboarding correspondence detected, no stage matched yet'}")
                c[1].caption(f"Ticket {ticket.ticket_id} · {ticket.manual_status or ticket.inferred_status or '—'}")
            else:
                c[1].caption("No onboarding-category ticket yet — only Annual Audit / Other tickets on file.")
            c[2].metric("Other tickets", r["other_ticket_count"])
            if ticket and can("drilldown_tickets") and st.button("Open", key=f"open_{ent.id}"):
                st.session_state["active_ticket"] = ticket.id
                st.session_state["nav"] = "C · Ticket Drill-Down"
                st.rerun()


# --------------------------------------------------------------------------- #
# Manual ingest (MODE 3)
# --------------------------------------------------------------------------- #
def page_manual_ingest(s):
    st.header("Manual ingest (Mode 3)")
    st.caption("Paste raw email headers+body, or upload a single .eml. Stays local.")
    up = st.file_uploader("Upload .eml", type=["eml"])
    raw = st.text_area("…or paste raw email (headers + blank line + body)", height=240)
    if st.button("Ingest"):
        from connectors.manual_upload_connector import ManualUploadConnector
        from ingestion.mailbox_sync_service import ingest

        ss = get_session()
        try:
            if up is not None:
                conn = ManualUploadConnector(raw_bytes=up.getvalue(), source_label=up.name)
            elif raw.strip():
                conn = ManualUploadConnector(raw_text=raw)
            else:
                st.warning("Provide a file or paste an email.")
                ss.close()
                return
            stats = ingest(ss, conn)
            ss.commit()
            st.success(f"Ingested: inserted={stats['inserted']} duplicates={stats['duplicates']} failed={stats['failed']}")
        except Exception as exc:  # noqa: BLE001
            ss.rollback()
            st.error(f"Error: {exc}")
        finally:
            ss.close()


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def page_admin(s):
    st.header("Admin")
    from db.models import SlaRule, User

    tabs = st.tabs(["Users", "SLA rules", "Entities", "Config"])
    with tabs[0]:
        st.subheader("Users")
        users = s.query(User).all()
        st.dataframe(pd.DataFrame([{
            "Email": u.email, "Name": u.name, "Role": ROLE_LABELS.get(u.role, u.role),
            "Internal": u.internal, "Active": u.is_active, "Last login": _fmt(u.last_login_at),
        } for u in users]), use_container_width=True, hide_index=True)
        with st.form("new_user"):
            st.markdown("**Create user**")
            c = st.columns(3)
            email = c[0].text_input("Email")
            name = c[1].text_input("Name")
            role = c[2].selectbox("Role", list(ROLE_LABELS))
            c = st.columns(3)
            dept = c[0].text_input("Department")
            internal = c[1].checkbox("Internal user", value=True)
            pw = c[2].text_input("Password", type="password")
            if st.form_submit_button("Create") and email and pw:
                from auth.users import create_user
                ss = get_session()
                try:
                    create_user(ss, email=email, name=name or email, role=role,
                                password=pw, department=dept or None, internal=internal)
                    ss.commit()
                    st.success(f"Created {email}")
                except Exception as exc:  # noqa: BLE001
                    ss.rollback()
                    st.error(str(exc))
                finally:
                    ss.close()
                st.rerun()
    with tabs[1]:
        st.subheader("SLA rules")
        rules = s.query(SlaRule).all()
        st.dataframe(pd.DataFrame([{
            "Priority": r.priority, "Category": r.category, "Due (hrs)": r.due_hours,
            "At-risk (hrs)": r.at_risk_hours,
        } for r in rules]), use_container_width=True, hide_index=True)
        st.caption("Edit SLA thresholds via .env defaults or extend here in a future iteration.")
    with tabs[2]:
        st.subheader("Requesting entities (AUA/KUA onboarding extension)")
        entities = svc.list_entities(s)
        if entities:
            st.dataframe(pd.DataFrame([{
                "Name": e.name, "Type": e.entity_type, "CIN": e.cin, "PAN": e.pan,
                "TAN": e.tan, "GSTIN": e.gstin, "Domains": e.known_domains,
                "Auto-detected": e.auto_created,
            } for e in entities]), use_container_width=True, hide_index=True)
        else:
            st.caption("No entities yet — created automatically from onboarding correspondence, "
                      "or manually below.")
        if can("manage_onboarding"):
            with st.form("new_entity"):
                st.markdown("**Register an entity**")
                c = st.columns(3)
                name = c[0].text_input("Name")
                etype = c[1].selectbox("Type", ENTITY_TYPES)
                domains = c[2].text_input("Known domains (comma-separated)")
                c = st.columns(4)
                cin = c[0].text_input("CIN")
                pan = c[1].text_input("PAN")
                tan = c[2].text_input("TAN")
                gstin = c[3].text_input("GSTIN")
                if st.form_submit_button("Create") and name:
                    ss = get_session()
                    try:
                        svc.create_or_update_entity(
                            ss, UserObj(current_user()), name=name, entity_type=etype,
                            known_domains=domains or None, cin=cin or None, pan=pan or None,
                            tan=tan or None, gstin=gstin or None)
                        ss.commit()
                        st.success(f"Created {name}")
                    except Exception as exc:  # noqa: BLE001
                        ss.rollback()
                        st.error(str(exc))
                    finally:
                        ss.close()
                    st.rerun()

    with tabs[3]:
        st.subheader("Configuration (read-only view of .env-derived settings)")
        st.json({
            "database_url": settings.database_url,
            "ingestion_mode": settings.ingestion_mode,
            "internal_domains": settings.internal_domains,
            "common_mailboxes": settings.common_mailboxes,
            "stale_days": settings.stale_days,
            "store_raw_email": settings.store_raw_email,
            "session_timeout_minutes": settings.session_timeout_minutes,
        })
        st.caption("Change these in `.env` and restart. Mailbox secrets are never shown.")


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def page_audit(s):
    st.header("Audit log")
    logs = svc.audit_logs(s)
    st.dataframe(pd.DataFrame([{
        "Time": _fmt(l.timestamp), "User": l.user_label, "Action": l.action_type,
        "Entity": f"{l.entity_type}:{l.entity_id}", "Old": (l.old_value or "")[:80],
        "New": (l.new_value or "")[:80],
    } for l in logs]), use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _fmt(d) -> str:
    d = svc._aware(d)
    return d.strftime("%Y-%m-%d %H:%M") if d else "—"


def _duration(a, b) -> str:
    a, b = svc._aware(a), svc._aware(b)
    if not a or not b:
        return "—"
    secs = (b - a).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)} min"
    if secs < 86400:
        return f"{secs / 3600:.1f} hrs"
    return f"{secs / 86400:.1f} days"


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #
PAGES = [
    ("A · Executive Overview", "view_dashboards", page_executive),
    ("B · Work Queue", "view_tickets", page_work_queue),
    ("C · Ticket Drill-Down", "drilldown_tickets", page_drilldown),
    ("D · Ageing & SLA", "view_dashboards", page_ageing),
    ("E · Owner / Team Performance", "view_dashboards", page_owner_perf),
    ("F · Category / Request Type", "view_dashboards", page_category),
    ("G · Mailbox Sync & Data Quality", "view_dashboards", page_data_quality),
    ("H · AUA/KUA Onboarding", "view_dashboards", page_onboarding),
    ("Manual ingest", "configure_ingestion", page_manual_ingest),
    ("Admin", "manage_users", page_admin),
    ("Audit log", "view_audit_logs", page_audit),
]


def main():
    try:
        init_db()
    except Exception:
        pass

    if not current_user():
        login_view()
        return
    enforce_timeout()
    if not current_user():
        login_view()
        return

    u = current_user()
    with st.sidebar:
        st.markdown(f"### 📬 Mailbox Ops")
        st.markdown(f"**{u['name']}**  \n{u['email']}  \n`{ROLE_LABELS.get(u['role'], u['role'])}`")
        allowed = [(label, perm, fn) for (label, perm, fn) in PAGES if has_permission(u["role"], perm)]
        labels = [p[0] for p in allowed]
        default = st.session_state.get("nav", labels[0])
        if default not in labels:
            default = labels[0]
        choice = st.radio("Navigate", labels, index=labels.index(default))
        st.session_state["nav"] = choice
        st.divider()
        if st.button("Sign out"):
            logout()
            st.rerun()
        st.caption(f"Ingestion mode: **{settings.ingestion_mode}**")
        st.caption("Local only · no external AI · no cloud")

    page_fn = next(fn for (label, perm, fn) in allowed if label == choice)
    s = get_session()
    try:
        page_fn(s)
    finally:
        s.close()


if __name__ == "__main__":
    main()
