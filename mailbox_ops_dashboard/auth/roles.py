"""Role-based access control. Permissions are declared here and enforced in
the service layer (auth.users.require), NOT only in the UI."""
from __future__ import annotations

# Canonical permission strings
PERM = {
    "view_dashboards",
    "view_tickets",
    "drilldown_tickets",
    "assign_owner",
    "edit_contributing_agents",
    "correct_detected_agent",
    "edit_ticket_fields",       # department/category/priority
    "set_manual_status",
    "add_note",
    "add_closure_note",
    "configure_ingestion",
    "configure_mailbox",
    "configure_internal_domains",
    "configure_sla",
    "manage_users",
    "view_audit_logs",
    "manage_onboarding",        # requesting-entity linking, onboarding stage, re-application links
    "manage_tags",              # add/remove free-form ticket tags
    "configure_automation",     # create/edit/delete automation rules (triggers)
    "configure_macros",         # create/edit/delete macro presets
    "merge_split_tickets",      # merge two tickets, or split one ticket in two
}

ROLES: dict[str, set[str]] = {
    "admin": set(PERM),  # everything
    "manager": {
        "view_dashboards", "view_tickets", "drilldown_tickets",
        "assign_owner", "edit_contributing_agents", "correct_detected_agent",
        "edit_ticket_fields", "set_manual_status", "add_note", "add_closure_note",
        "view_audit_logs", "manage_onboarding", "manage_tags", "merge_split_tickets",
    },
    "senior_viewer": {  # leadership, read-only
        "view_dashboards", "view_tickets", "drilldown_tickets",
    },
    "analyst": {
        "view_dashboards", "view_tickets", "drilldown_tickets",
        "set_manual_status", "add_note", "manage_tags",
    },
    "auditor": {  # read-only history + audit
        "view_tickets", "drilldown_tickets", "view_audit_logs",
    },
}

ROLE_LABELS = {
    "admin": "Admin",
    "manager": "Manager / Supervisor",
    "senior_viewer": "Senior Viewer / Leadership",
    "analyst": "Analyst",
    "auditor": "Auditor",
}


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLES.get(role, set())


def permissions_for(role: str) -> set[str]:
    return ROLES.get(role, set())
