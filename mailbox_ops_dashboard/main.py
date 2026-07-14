"""Mailbox Operations Dashboard — CLI entrypoint.

Commands:
  python main.py init-db [--admin-email X --admin-password Y]
  python main.py ingest-eml --path ./data/eml_exports
  python main.py ingest-manual --file msg.eml
  python main.py sync-gmail [--query "newer_than:30d"]
  python main.py test-imap                    # diagnose NIC/IMAP connectivity, run first
  python main.py sync-imap [--since 2024-01-01]
  python main.py run-dashboard [--port 8501]
  python main.py create-user --email X --name N --role R [--password P]
  python main.py recalculate-tickets
  python main.py export-report [--out report.csv]
  python main.py seed-demo          # load bundled sample .eml for a quick demo
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# Allow `python main.py ...` from inside the package directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings  # noqa: E402
from db.database import session_scope  # noqa: E402


def cmd_init_db(args) -> None:
    from db.migrations_or_init import bootstrap

    bootstrap(admin_email=args.admin_email, admin_password=args.admin_password)
    print(f"✓ Database initialized at {settings.database_url}")
    if args.admin_email:
        print(f"✓ Admin user created: {args.admin_email}")
    else:
        print("  (No admin created. Run: python main.py create-user --role admin ...)")


def cmd_ingest_eml(args) -> None:
    from ingestion.export_parser import ingest_export_folder

    path = args.path or str(settings.eml_export_path)
    with session_scope() as s:
        stats = ingest_export_folder(s, path)
    _print_stats("Export (.eml/.mbox)", stats)


def cmd_ingest_manual(args) -> None:
    from connectors.manual_upload_connector import ManualUploadConnector
    from ingestion.mailbox_sync_service import ingest

    if args.file:
        raw = Path(args.file).read_bytes()
        conn = ManualUploadConnector(raw_bytes=raw, source_label=args.file)
    else:
        print("Paste the raw email (headers + body). End with Ctrl-D:")
        raw_text = sys.stdin.read()
        conn = ManualUploadConnector(raw_text=raw_text)
    with session_scope() as s:
        stats = ingest(s, conn)
    _print_stats("Manual", stats)


def cmd_sync_gmail(args) -> None:
    from connectors.gmail_connector import GmailConnector
    from ingestion.mailbox_sync_service import ingest

    conn = GmailConnector(query=args.query)
    ok, msg = conn.healthcheck()
    if not ok:
        print(f"✗ {msg}")
        return
    with session_scope() as s:
        stats = ingest(s, conn)
    _print_stats("Gmail (dev/test)", stats)


def cmd_sync_imap(args) -> None:
    import datetime as dt

    from connectors.imap_connector import ImapConnector
    from ingestion.mailbox_sync_service import ingest

    since = dt.date.fromisoformat(args.since) if args.since else None
    conn = ImapConnector(since=since)
    ok, msg = conn.healthcheck()
    if not ok:
        print(f"✗ {msg}")
        return
    with session_scope() as s:
        stats = ingest(s, conn)
    _print_stats("IMAP (NIC/intranet)", stats)


def cmd_test_imap(args) -> None:
    """Run BEFORE sync-imap on a machine with real UIDAI VPN/intranet access.
    Diagnoses reachability/auth step by step without ingesting anything —
    never prints email content or the configured password."""
    from connectors.imap_diagnostics import run_diagnostics

    print("Testing IMAP connectivity (config -> DNS -> TCP -> TLS -> LOGIN -> mailbox)...\n")
    steps = run_diagnostics()
    for step in steps:
        mark = "✓" if step.ok else "✗"
        print(f"{mark} {step.name}: {step.detail}")
    if steps and steps[-1].ok and all(s.ok for s in steps):
        print("\nAll checks passed. Safe to run: python main.py sync-imap")
    else:
        print("\nStopped at the first failing step above — fix that before proceeding.")


def cmd_run_dashboard(args) -> None:
    import subprocess

    app_path = Path(__file__).resolve().parent / "dashboard" / "app.py"
    print(f"Launching Streamlit dashboard on port {args.port} …")
    subprocess.run(["streamlit", "run", str(app_path), "--server.port", str(args.port)])


def cmd_create_user(args) -> None:
    from auth.users import create_user

    password = args.password or getpass.getpass("Password: ")
    with session_scope() as s:
        user = create_user(s, email=args.email, name=args.name or args.email,
                           role=args.role, department=args.department,
                           password=password, internal=not args.external)
        print(f"✓ Created user {user.email} (role={user.role})")


def cmd_recalculate(args) -> None:
    from tickets.ticket_builder import rebuild_tickets

    with session_scope() as s:
        stats = rebuild_tickets(s)
    print(f"✓ Recalculated: {stats['tickets']} tickets from {stats['threads']} threads")


def cmd_export_report(args) -> None:
    import csv

    from db.models import Ticket, User

    out = args.out or "ticket_report.csv"
    with session_scope() as s:
        rows = s.query(Ticket).all()
        owners = {u.id: u.email for u in s.query(User).all()}
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ticket_id", "subject", "requester_email", "primary_owner",
                        "department", "category", "priority", "manual_status",
                        "inferred_status", "pending_with", "ageing_days",
                        "sla_status", "last_email_at", "last_action_by",
                        "is_escalated", "is_reopened"])
            for t in rows:
                w.writerow([t.ticket_id, t.subject, t.requester_email,
                            owners.get(t.primary_owner_user_id, ""), t.department,
                            t.category, t.priority, t.manual_status, t.inferred_status,
                            t.pending_with, t.ageing_days, t.sla_status, t.last_email_at,
                            t.last_action_by_label, t.is_escalated, t.is_reopened])
    print(f"✓ Report written to {out} ({len(rows)} tickets)")


def cmd_seed_demo(args) -> None:
    from db.migrations_or_init import bootstrap
    from ingestion.export_parser import ingest_export_folder

    bootstrap()
    sample_dir = Path(__file__).resolve().parent / "tests" / "sample_eml"
    with session_scope() as s:
        # create a demo agent so known-user detection has something to match
        from auth.users import create_user
        from db.models import User

        if not s.query(User).filter_by(email="admin@uidai.gov.in").first():
            create_user(s, email="admin@uidai.gov.in", name="Demo Admin", role="admin",
                        password="admin123", internal=True)
        if not s.query(User).filter_by(email="agent.rao@uidai.gov.in").first():
            create_user(s, email="agent.rao@uidai.gov.in", name="Agent Rao", role="analyst",
                        password="agent123", internal=True)
        stats = ingest_export_folder(s, sample_dir)
    _print_stats("Demo seed", stats)
    print("\nDemo users:")
    print("  admin@uidai.gov.in / admin123   (admin)")
    print("  agent.rao@uidai.gov.in / agent123  (analyst)")
    print("\nRun:  python main.py run-dashboard")


def _print_stats(label: str, stats: dict) -> None:
    status = "✓" if stats.get("success") else "✗"
    print(f"{status} {label}: processed={stats['processed']} inserted={stats['inserted']} "
          f"duplicates={stats['duplicates']} failed={stats['failed']}")
    for err in stats.get("errors", []):
        print(f"    ! {err}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description="Mailbox Operations Dashboard CLI")
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("init-db", help="Create tables + seed SLA rules")
    q.add_argument("--admin-email")
    q.add_argument("--admin-password")
    q.set_defaults(func=cmd_init_db)

    q = sub.add_parser("ingest-eml", help="Ingest a folder of .eml/.mbox exports")
    q.add_argument("--path")
    q.set_defaults(func=cmd_ingest_eml)

    q = sub.add_parser("ingest-manual", help="Ingest a single email (file or stdin)")
    q.add_argument("--file")
    q.set_defaults(func=cmd_ingest_manual)

    q = sub.add_parser("sync-gmail", help="Sync from Gmail (dev/test only)")
    q.add_argument("--query", default="newer_than:30d")
    q.set_defaults(func=cmd_sync_gmail)

    q = sub.add_parser("sync-imap", help="Sync from IMAP (NIC/intranet, needs VPN)")
    q.add_argument("--since", help="YYYY-MM-DD")
    q.set_defaults(func=cmd_sync_imap)

    q = sub.add_parser("test-imap", help="Diagnose NIC/IMAP connectivity step by step (run before sync-imap)")
    q.set_defaults(func=cmd_test_imap)

    q = sub.add_parser("run-dashboard", help="Launch the Streamlit dashboard")
    q.add_argument("--port", type=int, default=8501)
    q.set_defaults(func=cmd_run_dashboard)

    q = sub.add_parser("create-user", help="Create a local user")
    q.add_argument("--email", required=True)
    q.add_argument("--name")
    q.add_argument("--role", default="analyst",
                   choices=["admin", "manager", "senior_viewer", "analyst", "auditor"])
    q.add_argument("--department")
    q.add_argument("--password")
    q.add_argument("--external", action="store_true", help="Mark as non-internal user")
    q.set_defaults(func=cmd_create_user)

    q = sub.add_parser("recalculate-tickets", help="Rebuild tickets/status/SLA")
    q.set_defaults(func=cmd_recalculate)

    q = sub.add_parser("export-report", help="Export tickets to CSV")
    q.add_argument("--out")
    q.set_defaults(func=cmd_export_report)

    q = sub.add_parser("seed-demo", help="Init DB + load bundled sample emails")
    q.set_defaults(func=cmd_seed_demo)

    return p


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
