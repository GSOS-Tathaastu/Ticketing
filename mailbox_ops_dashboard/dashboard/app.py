"""Streamlit dashboard — login, RBAC-gated navigation, dashboards A–G, and the
mandatory expandable ticket drill-down. Run via `python main.py run-dashboard`
or `streamlit run dashboard/app.py`."""
from __future__ import annotations

import datetime as dt
import json
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
from tickets import detection_config, sla_engine, stage_detection_engine, status_engine  # noqa: E402
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
    if settings.ingestion_mode == "live_sync" and not settings.imap_host:
        if can("configure_mailbox"):
            st.warning("⚠️ Live IMAP sync is selected but no IMAP host is configured yet — "
                      "go to **Admin → Settings** to finish setup.")
        else:
            st.warning("⚠️ Live IMAP sync isn't fully configured yet — ask an admin to finish setup "
                      "under Admin → Settings.")
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
_WQ_FILTER_KEYS = ["owner", "dept", "status", "inferred", "priority", "ageing", "lastfrom",
                   "category", "tag", "breached", "unassigned", "escalated", "unknown", "multi",
                   "contrib", "watching", "search"]


def _wq_clamp(key: str, options: list, default=None):
    """Guard against a loaded saved view referencing a value (e.g. an owner
    who no longer exists) that isn't in this run's option list — Streamlit
    raises if a selectbox's session_state value isn't among its options."""
    full_key = f"wq_{key}"
    if st.session_state.get(full_key) not in options:
        st.session_state[full_key] = default if default is not None else options[0]


def _wq_current_filters() -> dict:
    return {k: st.session_state.get(f"wq_{k}") for k in _WQ_FILTER_KEYS}


def _wq_apply_saved_view(filters: dict) -> None:
    for k, v in filters.items():
        if k in _WQ_FILTER_KEYS:
            st.session_state[f"wq_{k}"] = v


def page_work_queue(s):
    st.header("B · Work Queue")
    tickets = svc.all_tickets(s)
    omap = svc.owners_map(s)
    u = UserObj(current_user())

    watched_ids = svc.watched_ticket_ids(s, u.id)
    rows = []
    for t in tickets:
        owner = omap.get(t.primary_owner_user_id)
        entity = svc.get_entity(s, t.requesting_entity_id) if t.requesting_entity_id else None
        tags = svc.tags_for(s, t.id)
        attach_n = svc.attachment_count(s, t.id)
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
            "SLA": t.sla_status or "—", "Tags": ", ".join(tags) or "—",
            "📎": attach_n or "—", "👁": "✓" if t.id in watched_ids else "",
            "Action needed": _action_needed(t),
            "_owner_id": t.primary_owner_user_id, "_escalated": t.is_escalated,
            "_unassigned": t.primary_owner_user_id is None,
            "_multi": svc.contributor_count(s, t.id) > 1,
            "_unknown_agent": (not t.last_action_by_agent_id and bool(t.last_agent_email_at)),
            "_last_from": last_from, "_tags": tags, "_watching": t.id in watched_ids,
        })
    df = pd.DataFrame(rows)

    # --- Saved views (Zammad "Overviews") --------------------------------
    views = svc.list_saved_views(s, u)
    with st.expander("Saved views", expanded=False):
        vc = st.columns([3, 1])
        vopts = {f"{v.name}{' · shared' if v.is_shared else ''}": v.id for v in views}
        if vopts:
            vpick = vc[0].selectbox("Load a saved view", list(vopts))
            if vc[1].button("Load"):
                target = next(v for v in views if v.id == vopts[vpick])
                _wq_apply_saved_view(json.loads(target.filters_json))
                st.rerun()
        else:
            vc[0].caption("No saved views yet.")
        st.markdown("**Save current filters as a view**")
        sc = st.columns([2, 1, 1])
        new_view_name = sc[0].text_input("Name", key="wq_new_view_name")
        shared = sc[1].checkbox("Shared with everyone", key="wq_new_view_shared")
        if sc[2].button("Save view") and new_view_name.strip():
            name_to_save, filters_to_save = new_view_name.strip(), _wq_current_filters()
            _mutate(s, lambda ss: svc.create_saved_view(ss, u, name_to_save, filters_to_save, is_shared=shared))
        mine = [v for v in views if v.owner_user_id == u.id]
        if mine:
            dopts = {v.name: v.id for v in mine}
            dc = st.columns([3, 1])
            dpick = dc[0].selectbox("Delete a view you own", list(dopts), key="wq_delete_view_pick")
            if dc[1].button("Delete"):
                _mutate(s, lambda ss: svc.delete_saved_view(ss, u, dopts[dpick]))

    with st.expander("Filters", expanded=True):
        owner_opts = ["(all)", "Unassigned"] + sorted({r["Primary owner"] for r in rows if r["Primary owner"] != "—"})
        dept_opts = ["(all)"] + sorted({r["Department"] for r in rows})
        status_opts = ["(all)"] + sorted({r["Manual status"] for r in rows})
        inferred_opts = ["(all)"] + sorted({r["Inferred status"] for r in rows})
        priority_opts = ["(all)"] + PRIORITIES
        ageing_opts = ["(all)"] + sla_engine.AGEING_BUCKETS
        lastfrom_opts = ["(all)", "customer", "agent"]
        category_opts = ["(all)"] + sorted({r["Category"] for r in rows})
        tag_opts = ["(all)"] + svc.all_tags(s)
        for key, opts in (("owner", owner_opts), ("dept", dept_opts), ("status", status_opts),
                         ("inferred", inferred_opts), ("priority", priority_opts), ("ageing", ageing_opts),
                         ("lastfrom", lastfrom_opts), ("category", category_opts), ("tag", tag_opts)):
            _wq_clamp(key, opts)

        c = st.columns(4)
        f_owner = c[0].selectbox("Owner", owner_opts, key="wq_owner")
        f_dept = c[1].selectbox("Department", dept_opts, key="wq_dept")
        f_status = c[2].selectbox("Manual status", status_opts, key="wq_status")
        f_inferred = c[3].selectbox("Inferred status", inferred_opts, key="wq_inferred")
        c = st.columns(4)
        f_priority = c[0].selectbox("Priority", priority_opts, key="wq_priority")
        f_ageing = c[1].selectbox("Ageing bucket", ageing_opts, key="wq_ageing")
        f_lastfrom = c[2].selectbox("Last email from", lastfrom_opts, key="wq_lastfrom")
        f_category = c[3].selectbox("Category", category_opts, key="wq_category")
        c = st.columns(2)
        f_tag = c[0].selectbox("Tag", tag_opts, key="wq_tag")
        f_contrib = c[1].text_input("Contributing agent contains", key="wq_contrib")
        f_search = st.text_input("Search subject / requester / email body", key="wq_search")
        c = st.columns(6)
        f_breached = c[0].checkbox("SLA breached", key="wq_breached")
        f_unassigned = c[1].checkbox("Unassigned", key="wq_unassigned")
        f_escalated = c[2].checkbox("Escalated", key="wq_escalated")
        f_unknown = c[3].checkbox("Unknown agent", key="wq_unknown")
        f_multi = c[4].checkbox("Multiple agents", key="wq_multi")
        f_watching = c[5].checkbox("Watching only", key="wq_watching")

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
        if f_tag != "(all)":
            df = df[df["_tags"].apply(lambda tl: f_tag in tl)]
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
        if f_watching:
            df = df[df["_watching"]]
        if f_search.strip():
            matching = svc.search_ticket_ids(s, f_search)
            df = df[df["PK"].isin(matching)]
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
                    "Last email from", "Last email", "SLA", "Tags", "📎", "👁", "Action needed"]
    st.dataframe(df[display_cols] if not df.empty else df, use_container_width=True, hide_index=True)

    if not df.empty and can("drilldown_tickets"):
        st.divider()
        options = {f"{r['Ticket ID']} — {r['Subject']}": r["PK"] for _, r in df.iterrows()}
        pick = st.selectbox("Open ticket drill-down", ["(select)"] + list(options))
        if pick != "(select)":
            st.session_state["active_ticket"] = options[pick]
            st.session_state["nav"] = "C · Ticket Drill-Down"
            st.rerun()

    if not df.empty:
        _render_bulk_actions(s, u, df)


def _render_bulk_actions(s, u, df):
    """Macros / bulk actions (Zammad-style): apply one set of field changes to
    many selected tickets at once, or a saved reusable macro preset."""
    can_any = any(can(p) for p in ("set_manual_status", "edit_ticket_fields", "assign_owner",
                                   "manage_tags", "add_note"))
    if not can_any:
        return
    st.divider()
    with st.expander("Bulk actions / Macros", expanded=False):
        options = {f"{r['Ticket ID']} — {r['Subject']}": r["PK"] for _, r in df.iterrows()}
        picked_labels = st.multiselect("Tickets to apply to", list(options))
        picked_ids = [options[label] for label in picked_labels]
        if not picked_ids:
            st.caption("Select one or more tickets above.")
            return

        macros = svc.list_macros(s)
        if macros:
            mopts = {"— none, use fields below —": None} | {m.name: m.id for m in macros}
            mpick = st.selectbox("Apply a macro", list(mopts))
            if mopts[mpick] is not None and st.button("Apply macro to selected"):
                _mutate(s, lambda ss: svc.apply_macro(ss, u, mopts[mpick], picked_ids))
            st.markdown("— or set fields ad hoc —")

        c = st.columns(4)
        status = c[0].selectbox("Set manual status", ["(no change)"] + MANUAL_STATUSES[1:],
                                disabled=not can("set_manual_status"))
        priority = c[1].selectbox("Set priority", ["(no change)"] + PRIORITIES,
                                  disabled=not can("edit_ticket_fields"))
        category = c[2].text_input("Set category", disabled=not can("edit_ticket_fields"))
        department = c[3].text_input("Set department", disabled=not can("edit_ticket_fields"))
        c = st.columns(3)
        owner_opts = {"(no change)": svc.NO_CHANGE, "— unassigned —": None} | {
            f"{x.name} <{x.email}>": x.id for x in svc.internal_users(s)}
        owner_pick = c[0].selectbox("Set owner", list(owner_opts), disabled=not can("assign_owner"))
        add_tag = c[1].text_input("Add tag", disabled=not can("manage_tags"))
        note = c[2].text_input("Add note", disabled=not can("add_note"))

        if st.button(f"Apply to {len(picked_ids)} selected ticket(s)"):
            _mutate(s, lambda ss: svc.apply_bulk_actions(
                ss, u, picked_ids,
                status=None if status == "(no change)" else status,
                priority=None if priority == "(no change)" else priority,
                category=category.strip() or None, department=department.strip() or None,
                owner_id=owner_opts[owner_pick], add_tag=add_tag.strip() or None,
                note=note.strip() or None,
            ))


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
    uo = UserObj(current_user())

    # Agent collision detection (advisory only) — touch the lock marker every
    # time this page renders for this ticket, and warn if someone else's
    # touch is still recent. Its own tiny session/commit: not a data
    # mutation worth _mutate's "Saved." toast or a full-page audit-log entry.
    lock_ss = get_session()
    try:
        collision = svc.touch_ticket_lock(lock_ss, uo, svc.get_ticket(lock_ss, ticket.id))
        lock_ss.commit()
    finally:
        lock_ss.close()
    if collision:
        st.warning(f"⚠️ Also opened by **{collision['email']}** {collision['seconds_ago']}s ago — "
                  "coordinate before saving changes so you don't overwrite each other.")

    # Watchers (Zammad-style CC/subscriber) — self-service toggle.
    watching = svc.is_watching(s, ticket.id, uo.id)
    wc = st.columns([1, 5])
    if wc[0].button("👁 Unwatch" if watching else "👁 Watch"):
        _mutate(s, lambda ss: (svc.unwatch_ticket if watching else svc.watch_ticket)(
            ss, uo, svc.get_ticket(ss, ticket.id)))
    watchers = svc.watchers_for(s, ticket.id)
    if watchers:
        wc[1].caption("Watching: " + ", ".join(w.name or w.email for w in watchers))

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

    # Tags (Zammad-style free-form labels) --------------------------------
    with st.expander("🏷️ Tags", expanded=False):
        tags = svc.tags_for(s, ticket.id)
        st.markdown(", ".join(f"`{t}`" for t in tags) if tags else "_No tags._")
        if can("manage_tags"):
            uo = UserObj(current_user())
            c = st.columns([2, 1])
            new_tag = c[0].text_input("Add tag", key="new_tag_input")
            if c[1].button("Add tag") and new_tag.strip():
                _mutate(s, lambda ss: svc.tag_ticket(ss, uo, svc.get_ticket(ss, ticket.id), new_tag.strip()))
            if tags:
                rm = st.selectbox("Remove a tag", ["(select)"] + tags, key="rm_tag_pick")
                if rm != "(select)" and st.button("Remove tag"):
                    _mutate(s, lambda ss: svc.untag_ticket(ss, uo, svc.get_ticket(ss, ticket.id), rm))

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
    if kind in ("status_change", "owner_change", "agent_correction", "merge", "split", "automation"):
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
    st.caption(meta)
    if e.has_attachment:
        atts = svc.load_json(e.attachment_metadata)
        for a in atts:
            name = a.get("filename") or "(unnamed)"
            ctype = a.get("content_type") or "unknown type"
            size = a.get("size")
            st.caption(f"　📎 {name} · {ctype}" + (f" · {_fmt_size(size)}" if size else ""))
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
    if can("merge_split_tickets"):
        tab_labels.append("Merge / Split")
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
        if can("edit_ticket_fields"):
            st.caption("Correct requester — for a garbled name, or the wrong participant picked "
                      "out of a multi-recipient thread. Locks against being overwritten by rebuilds.")
            c = st.columns(2)
            req_email = c[0].text_input("Requester email", ticket.requester_email or "")
            req_name = c[1].text_input("Requester name", ticket.requester_name or "")
            if st.button("Save requester"):
                _mutate(s, lambda ss: svc.correct_requester(ss, uo, svc.get_ticket(ss, ticket.id),
                                                            req_email.strip() or None, req_name.strip() or None))

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
        with tabs[tab_labels.index("AUA/KUA Onboarding")]:
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

    if can("merge_split_tickets"):
        with tabs[tab_labels.index("Merge / Split")]:
            st.markdown("**Merge another ticket into this one** — its emails and internal notes "
                       "move here; the other ticket is closed and disappears from the Work Queue.")
            other_tickets = [t for t in svc.all_tickets(s) if t.id != ticket.id]
            mopts = {f"{t.ticket_id} — {(t.subject or '')[:50]}": t.id for t in other_tickets}
            if mopts:
                mpick = st.selectbox("Ticket to merge in", list(mopts))
                if st.button("Merge into this ticket"):
                    _mutate(s, lambda ss: svc.merge_tickets(
                        ss, uo, svc.get_ticket(ss, ticket.id), svc.get_ticket(ss, mopts[mpick])))
            else:
                st.caption("No other tickets to merge.")

            st.divider()
            st.markdown("**Split off part of this ticket into a new one** — pick the emails that "
                       "actually belong to a separate issue.")
            eopts = {f"{_fmt(e.sent_at)} — {(e.subject or '')[:50]} ({e.direction})": e.email_id
                    for e in events if e.event_kind == "email" and e.email_id}
            picked = st.multiselect("Emails to move to a new ticket", list(eopts))
            new_subj = st.text_input("New ticket subject (optional)")
            if picked and st.button("Split off into new ticket"):
                _mutate(s, lambda ss: svc.split_ticket(
                    ss, uo, svc.get_ticket(ss, ticket.id),
                    [eopts[p] for p in picked], new_subj.strip() or None))


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
    health = svc.sync_health(s)
    if health["applicable"]:
        if health["status"] == svc.SYNC_OK:
            st.success(f"✓ Sync healthy — last successful sync {health['age_hours']}h ago "
                      f"(threshold {health['threshold_hours']}h).")
        elif health["status"] == svc.SYNC_STALE:
            st.error(f"✗ Sync is STALE — last successful sync {health['age_hours']}h ago, "
                    f"threshold is {health['threshold_hours']}h. Check the scheduled sync job.")
        else:
            st.error("✗ Never synced successfully yet in live_sync mode.")
        if health["last_attempt_success"] is False:
            st.warning(f"⚠️ Most recent sync attempt at {_fmt(health['last_attempt_at'])} failed — "
                      "see the sync run's notes, or re-run `sync-imap`/`sync-gmail` to see the error.")
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
    st.divider()
    st.subheader("Full data export")
    st.caption("Every dashboard's data as one .zip of CSVs — tickets, executive overview, "
              "ageing/SLA, owner performance, category breakdown, requesting entities.")
    st.download_button("⬇️ Download full report (.zip)", data=svc.build_full_export_zip(s),
                       file_name=f"mailbox_ops_full_report_{_now().strftime('%Y%m%d_%H%M')}.zip",
                       mime="application/zip")


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

    tabs = st.tabs(["Users", "SLA rules", "Entities", "Settings", "Automation rules", "Macros",
                   "Detection keywords"])
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
        rules = svc.list_sla_rules(s)
        st.dataframe(pd.DataFrame([{
            "ID": r.id, "Priority": r.priority, "Category": r.category, "Due (hrs)": r.due_hours,
            "At-risk (hrs)": r.at_risk_hours,
        } for r in rules]), use_container_width=True, hide_index=True)
        if can("configure_sla") and rules:
            st.markdown("**Edit a rule**")
            ropts = {f"{r.priority} / {r.category} (id={r.id})": r.id for r in rules}
            rpick = st.selectbox("Rule", list(ropts))
            cur_rule = next(r for r in rules if r.id == ropts[rpick])
            c = st.columns(2)
            due2 = c[0].number_input("Due (hrs)", min_value=1, value=cur_rule.due_hours)
            risk2 = c[1].number_input("At-risk window (hrs before due)", min_value=1, value=cur_rule.at_risk_hours)
            if st.button("Save rule"):
                _mutate(s, lambda ss: svc.update_sla_rule(ss, UserObj(current_user()), ropts[rpick],
                                                          due_hours=int(due2), at_risk_hours=int(risk2)))
        if can("configure_sla"):
            st.markdown("**Add a category-specific rule** _(falls back to the priority-only `*` rule "
                       "when no exact category match exists)_")
            with st.form("new_sla_rule"):
                c = st.columns(4)
                prio2 = c[0].selectbox("Priority", PRIORITIES, key="new_sla_priority")
                cat2 = c[1].text_input("Category (\"*\" for all)", value="*")
                due3 = c[2].number_input("Due (hrs)", min_value=1, value=48)
                risk3 = c[3].number_input("At-risk (hrs)", min_value=1, value=8)
                if st.form_submit_button("Add rule"):
                    ss = get_session()
                    try:
                        svc.create_sla_rule(ss, UserObj(current_user()), priority=prio2, category=cat2,
                                            due_hours=int(due3), at_risk_hours=int(risk3))
                        ss.commit()
                        st.success("Rule added")
                    except Exception as exc:  # noqa: BLE001
                        ss.rollback()
                        st.error(str(exc))
                    finally:
                        ss.close()
                    st.rerun()
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
            with st.expander("Bulk import entities (CSV)", expanded=False):
                st.caption("Matches an existing entity by PAN first, then by exact name; only "
                          "overwrites a field the CSV row actually has a value for.")
                st.download_button("⬇️ Download CSV template", data=svc.ENTITY_CSV_TEMPLATE,
                                   file_name="entities_template.csv", mime="text/csv")
                up = st.file_uploader("Upload entities CSV", type=["csv"], key="entities_csv_upload")
                if up is not None and st.button("Import"):
                    ss = get_session()
                    try:
                        result = svc.import_entities_csv(ss, UserObj(current_user()), up.getvalue())
                        ss.commit()
                        st.success(f"Created {result['created']}, updated {result['updated']}, "
                                  f"skipped {result['skipped']}.")
                        for err in result["errors"]:
                            st.caption(f"⚠️ {err}")
                    except Exception as exc:  # noqa: BLE001
                        ss.rollback()
                        st.error(str(exc))
                    finally:
                        ss.close()
                    st.rerun()

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

            if entities:
                st.markdown("**Correct an existing entity** _(e.g. an auto-detected name/PAN that's wrong)_")
                eopts = {f"{e.name} (id={e.id})": e.id for e in entities}
                epick = st.selectbox("Entity", list(eopts), key="edit_entity_pick")
                cur = svc.get_entity(s, eopts[epick])
                with st.form("edit_entity"):
                    c = st.columns(3)
                    name2 = c[0].text_input("Name", cur.name)
                    etype2 = c[1].selectbox("Type", ENTITY_TYPES,
                                            index=ENTITY_TYPES.index(cur.entity_type) if cur.entity_type in ENTITY_TYPES else 0)
                    domains2 = c[2].text_input("Known domains (comma-separated)", cur.known_domains or "")
                    c = st.columns(4)
                    cin2 = c[0].text_input("CIN", cur.cin or "")
                    pan2 = c[1].text_input("PAN", cur.pan or "")
                    tan2 = c[2].text_input("TAN", cur.tan or "")
                    gstin2 = c[3].text_input("GSTIN", cur.gstin or "")
                    if st.form_submit_button("Save changes"):
                        ss = get_session()
                        try:
                            svc.create_or_update_entity(
                                ss, UserObj(current_user()), entity_id=cur.id, name=name2,
                                entity_type=etype2, known_domains=domains2 or None,
                                cin=cin2 or None, pan=pan2 or None, tan=tan2 or None, gstin=gstin2 or None)
                            ss.commit()
                            st.success(f"Updated {name2}")
                        except Exception as exc:  # noqa: BLE001
                            ss.rollback()
                            st.error(str(exc))
                        finally:
                            ss.close()
                        st.rerun()

    with tabs[3]:
        st.subheader("Settings")
        st.caption("Admin-only, audit-logged. Values here override `.env` immediately, no restart "
                  "needed — `.env` is only the initial bootstrap default now.")
        st.info("🔒 **IMAP password is never stored here or shown in this UI** — it stays in `.env` "
               "on the host machine only, same as before. Everything below is non-secret.")
        cur = svc.get_app_settings(s)
        editable = can("configure_mailbox") or can("configure_internal_domains") or can("configure_sla")

        with st.form("app_settings"):
            st.markdown("**Mailbox connection** _(IMAP — requires `configure_mailbox`)_")
            c = st.columns(3)
            imap_host2 = c[0].text_input("IMAP host", cur["imap_host"], disabled=not can("configure_mailbox"))
            imap_port2 = c[1].number_input("IMAP port", value=cur["imap_port"], disabled=not can("configure_mailbox"))
            imap_user2 = c[2].text_input("IMAP user", cur["imap_user"], disabled=not can("configure_mailbox"))
            c = st.columns(3)
            imap_mailbox2 = c[0].text_input("IMAP mailbox/folder", cur["imap_mailbox"], disabled=not can("configure_mailbox"))
            imap_ssl2 = c[1].checkbox("Use SSL", value=cur["imap_use_ssl"], disabled=not can("configure_mailbox"))
            mode_opts = ["export", "live_sync", "manual"]
            mode2 = c[2].selectbox("Ingestion mode", mode_opts,
                                   index=mode_opts.index(cur["ingestion_mode"]) if cur["ingestion_mode"] in mode_opts else 0,
                                   disabled=not can("configure_ingestion"))

            st.markdown("**Identity** _(requires `configure_internal_domains`)_")
            c = st.columns(2)
            domains2 = c[0].text_area("Internal domains (comma-separated)",
                                      ", ".join(cur["internal_domains"]), disabled=not can("configure_internal_domains"))
            mailboxes2 = c[1].text_area("Common/shared mailboxes (comma-separated)",
                                        ", ".join(cur["common_mailboxes"]), disabled=not can("configure_internal_domains"))

            st.markdown("**Thresholds** _(requires `configure_sla` / `configure_ingestion`)_")
            c = st.columns(4)
            stale2 = c[0].number_input("Stale after (days)", min_value=1, value=cur["stale_days"], disabled=not can("configure_sla"))
            due2 = c[1].number_input("Default SLA due (hrs)", min_value=1, value=cur["default_sla_hours"], disabled=not can("configure_sla"))
            risk2 = c[2].number_input("Default at-risk window (hrs)", min_value=1, value=cur["sla_at_risk_hours"], disabled=not can("configure_sla"))
            timeout2 = c[3].number_input("Session timeout (min)", min_value=1, value=cur["session_timeout_minutes"], disabled=not can("configure_mailbox"))
            c = st.columns(4)
            sync_age2 = c[0].number_input("Sync health threshold (hrs)", min_value=1,
                                          value=cur["sync_max_age_hours"], disabled=not can("configure_ingestion"))

            if st.form_submit_button("Save settings", disabled=not editable):
                fields = {}
                if can("configure_mailbox"):
                    fields.update(imap_host=imap_host2, imap_port=int(imap_port2), imap_user=imap_user2,
                                 imap_mailbox=imap_mailbox2, imap_use_ssl=imap_ssl2,
                                 session_timeout_minutes=int(timeout2))
                if can("configure_ingestion"):
                    fields["ingestion_mode"] = mode2
                    fields["sync_max_age_hours"] = int(sync_age2)
                if can("configure_internal_domains"):
                    fields["internal_domains"] = [d.strip() for d in domains2.split(",") if d.strip()]
                    fields["common_mailboxes"] = [m.strip() for m in mailboxes2.split(",") if m.strip()]
                if can("configure_sla"):
                    fields.update(stale_days=int(stale2), default_sla_hours=int(due2), sla_at_risk_hours=int(risk2))
                _mutate(s, lambda ss: svc.update_app_settings(ss, UserObj(current_user()), **fields))

        if not editable:
            st.caption("Read-only for your role.")

    with tabs[4]:
        st.subheader("Automation rules (triggers)")
        st.caption("Evaluated once, only when a NEW ticket is created — never on later rebuilds, "
                  "so a rule can never silently overwrite a field a human edited afterwards.")
        rules = svc.list_automation_rules(s)
        if rules:
            st.dataframe(pd.DataFrame([{
                "ID": r.id, "Name": r.name, "Active": r.is_active, "Order": r.run_order,
                "If": f'{r.condition_field} {r.condition_op} "{r.condition_value}"',
                "Set category": r.action_set_category or "—", "Set priority": r.action_set_priority or "—",
                "Set dept": r.action_set_department or "—", "Assign owner": r.action_assign_owner_email or "—",
                "Add tag": r.action_add_tag or "—",
            } for r in rules]), use_container_width=True, hide_index=True)
        else:
            st.caption("No automation rules yet.")

        if can("configure_automation"):
            with st.form("new_automation_rule"):
                st.markdown("**Add a rule**")
                c = st.columns(4)
                name = c[0].text_input("Name")
                active = c[1].checkbox("Active", value=True)
                order = c[2].number_input("Run order", value=0, step=1)
                cond_field = c[3].selectbox("Condition field",
                                            ["subject", "body", "requester_email", "requester_domain"])
                c = st.columns(2)
                cond_op = c[0].selectbox("Condition", ["contains", "equals"])
                cond_val = c[1].text_input("Condition value")
                st.markdown("_Actions — leave blank to skip_")
                c = st.columns(3)
                act_cat = c[0].text_input("Set category")
                act_prio = c[1].selectbox("Set priority", [""] + PRIORITIES)
                act_dept = c[2].text_input("Set department")
                c = st.columns(2)
                act_owner = c[0].text_input("Assign owner (email)")
                act_tag = c[1].text_input("Add tag")
                if st.form_submit_button("Create rule") and name and cond_val:
                    ss = get_session()
                    try:
                        svc.create_automation_rule(
                            ss, UserObj(current_user()), name=name, is_active=active, run_order=int(order),
                            condition_field=cond_field, condition_op=cond_op, condition_value=cond_val,
                            action_set_category=act_cat or None, action_set_priority=act_prio or None,
                            action_set_department=act_dept or None, action_assign_owner_email=act_owner or None,
                            action_add_tag=act_tag or None)
                        ss.commit()
                        st.success(f"Created rule '{name}'")
                    except Exception as exc:  # noqa: BLE001
                        ss.rollback()
                        st.error(str(exc))
                    finally:
                        ss.close()
                    st.rerun()

            if rules:
                st.markdown("**Toggle / delete a rule**")
                ropts = {f"{r.name} (id={r.id})": r.id for r in rules}
                rpick = st.selectbox("Rule", list(ropts), key="automation_rule_pick")
                cur_rule = next(r for r in rules if r.id == ropts[rpick])
                c = st.columns(2)
                if c[0].button("Toggle active"):
                    _mutate(s, lambda ss: svc.update_automation_rule(
                        ss, UserObj(current_user()), ropts[rpick], is_active=not cur_rule.is_active))
                if c[1].button("Delete rule"):
                    _mutate(s, lambda ss: svc.delete_automation_rule(ss, UserObj(current_user()), ropts[rpick]))

    with tabs[5]:
        st.subheader("Macros")
        st.caption("A reusable, named bundle of field changes agents can apply to one or more "
                  "tickets at once from the Work Queue's Bulk actions panel.")
        macros = svc.list_macros(s)
        if macros:
            st.dataframe(pd.DataFrame([{
                "ID": m.id, "Name": m.name, "Status": m.action_set_status or "—",
                "Priority": m.action_set_priority or "—", "Category": m.action_set_category or "—",
                "Department": m.action_set_department or "—", "Owner": m.action_assign_owner_email or "—",
                "Tag": m.action_add_tag or "—", "Note": (m.action_add_note or "—")[:40],
            } for m in macros]), use_container_width=True, hide_index=True)
        else:
            st.caption("No macros yet.")

        if can("configure_macros"):
            with st.form("new_macro"):
                st.markdown("**Add a macro** _(leave a field blank to leave it unchanged when applied)_")
                name = st.text_input("Name")
                c = st.columns(4)
                status = c[0].selectbox("Set status", [""] + MANUAL_STATUSES[1:])
                priority = c[1].selectbox("Set priority", [""] + PRIORITIES)
                category = c[2].text_input("Set category")
                department = c[3].text_input("Set department")
                c = st.columns(2)
                owner_email = c[0].text_input("Assign owner (email)")
                tag = c[1].text_input("Add tag")
                note = st.text_area("Add note")
                if st.form_submit_button("Create macro") and name:
                    ss = get_session()
                    try:
                        svc.create_macro(
                            ss, UserObj(current_user()), name=name,
                            action_set_status=status or None, action_set_priority=priority or None,
                            action_set_category=category or None, action_set_department=department or None,
                            action_assign_owner_email=owner_email or None, action_add_tag=tag or None,
                            action_add_note=note or None)
                        ss.commit()
                        st.success(f"Created macro '{name}'")
                    except Exception as exc:  # noqa: BLE001
                        ss.rollback()
                        st.error(str(exc))
                    finally:
                        ss.close()
                    st.rerun()

            if macros:
                st.markdown("**Delete a macro**")
                mopts = {f"{m.name} (id={m.id})": m.id for m in macros}
                mpick = st.selectbox("Macro", list(mopts), key="macro_delete_pick")
                if st.button("Delete macro"):
                    _mutate(s, lambda ss: svc.delete_macro(ss, UserObj(current_user()), mopts[mpick]))

    with tabs[6]:
        st.subheader("Detection keywords")
        st.caption("Tune the escalation/closure status-inference keywords and the AUA/KUA onboarding "
                  "classifier without a redeploy. Takes effect on the next ticket rebuild "
                  "(recalculate-tickets, or the next email sync). Matched as literal phrases, "
                  "case-insensitive — not regex.")
        kws = svc.list_detection_keywords(s)
        for category, label in detection_config.CATEGORY_LABELS.items():
            st.markdown(f"**{label}**")
            cat_kws = [k for k in kws if k.category == category]
            if cat_kws:
                st.dataframe(pd.DataFrame([{
                    "Phrase": k.phrase, "Active": k.is_active,
                } for k in cat_kws]), use_container_width=True, hide_index=True)
            else:
                st.caption("_(using built-in defaults — nothing customized yet)_")
            if can("configure_detection_keywords"):
                c = st.columns([3, 1])
                new_phrase = c[0].text_input(f"Add phrase to {category}", key=f"kw_new_{category}")
                if c[1].button("Add", key=f"kw_add_{category}") and new_phrase.strip():
                    _mutate(s, lambda ss, cat=category, ph=new_phrase: svc.add_detection_keyword(
                        ss, UserObj(current_user()), cat, ph))
                if cat_kws:
                    kopts = {k.phrase: k.id for k in cat_kws}
                    c = st.columns([3, 1, 1])
                    kpick = c[0].selectbox("Phrase", list(kopts), key=f"kw_pick_{category}")
                    picked = next(k for k in cat_kws if k.id == kopts[kpick])
                    if c[1].button("Toggle active", key=f"kw_toggle_{category}"):
                        _mutate(s, lambda ss, kid=picked.id, active=not picked.is_active:
                               svc.set_detection_keyword_active(ss, UserObj(current_user()), kid, active))
                    if c[2].button("Delete", key=f"kw_del_{category}"):
                        _mutate(s, lambda ss, kid=picked.id: svc.delete_detection_keyword(
                            ss, UserObj(current_user()), kid))
            st.divider()


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
def _fmt_size(num_bytes) -> str:
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


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

    from config.runtime_settings import apply_db_overrides

    _s = get_session()
    try:
        apply_db_overrides(_s)  # admin-saved settings take effect on every render, no restart needed
    finally:
        _s.close()

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
