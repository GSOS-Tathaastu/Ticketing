"""User management + authentication + backend-level permission enforcement +
audit logging. Every mutating service should call `require(...)` and
`write_audit(...)`."""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy.orm import Session

from auth.roles import ROLES, has_permission
from auth.security import hash_password, verify_password
from db.models import AuditLog, User


class PermissionError_(Exception):
    """Raised when a user lacks a required permission (backend enforcement)."""


def create_user(session: Session, *, email: str, name: str, role: str,
                password: str, department: str | None = None,
                internal: bool = True) -> User:
    if role not in ROLES:
        raise ValueError(f"Unknown role '{role}'. Valid: {sorted(ROLES)}")
    email = email.strip().lower()
    if session.query(User).filter_by(email=email).first():
        raise ValueError(f"User '{email}' already exists")
    user = User(
        email=email, name=name, role=role, department=department,
        internal=internal, password_hash=hash_password(password), is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.query(User).filter_by(email=email.strip().lower(), is_active=True).first()
    if user and verify_password(password, user.password_hash):
        user.last_login_at = dt.datetime.now(dt.timezone.utc)
        session.flush()
        return user
    return None


def require(user: User | None, permission: str) -> None:
    """Backend permission gate. Raises PermissionError_ if not allowed."""
    if user is None or not has_permission(user.role, permission):
        role = getattr(user, "role", "anonymous")
        raise PermissionError_(f"Role '{role}' lacks permission '{permission}'")


def write_audit(session: Session, *, user: User | None, action_type: str,
                entity_type: str, entity_id, old_value=None, new_value=None) -> None:
    def _ser(v):
        if v is None or isinstance(v, str):
            return v
        try:
            return json.dumps(v, default=str)
        except TypeError:
            return str(v)

    session.add(AuditLog(
        user_id=getattr(user, "id", None),
        user_label=getattr(user, "email", "system"),
        action_type=action_type,
        entity_type=entity_type,
        entity_id=str(entity_id),
        old_value=_ser(old_value),
        new_value=_ser(new_value),
    ))
