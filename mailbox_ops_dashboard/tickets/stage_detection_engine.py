"""Onboarding-stage inference for AUA/KUA/Sub-AUA/Sub-KUA tickets (DESIGN.md
§13.4). Structurally identical to `status_engine.py`: a rule-based keyword
cascade producing `inferred_onboarding_stage`, kept separate from the
human-settable `manual_onboarding_stage`. Not trusted silently — keyword
matching on real UIDAI correspondence needs tuning against actual samples,
and a supervisor can always correct it.

Reaching the final stage does NOT auto-close the ticket — closing stays a
manual `set_manual_status` action.
"""
from __future__ import annotations

import re

# Ordered stages — later stages imply earlier ones are complete.
APPLICATION_SUBMITTED = "Application Submitted"
AGREEMENT_IN_PRINCIPLE = "Agreement & In-Principle Approval"
AUDIT_COMPLIANCE = "Audit / Compliance Certification"
PRE_PRODUCTION = "Pre-Production Testing"
PRODUCTION_LIVE = "Production Go-Live"

STAGES = [APPLICATION_SUBMITTED, AGREEMENT_IN_PRINCIPLE, AUDIT_COMPLIANCE, PRE_PRODUCTION, PRODUCTION_LIVE]

_STAGE_KEYWORDS: dict[str, re.Pattern] = {
    PRODUCTION_LIVE: re.compile(
        r"\b(production access granted|go[\s-]?live|now live in production|"
        r"production environment (?:is )?activated|production credentials)\b", re.IGNORECASE),
    PRE_PRODUCTION: re.compile(
        r"\b(pre-?production (?:testing|environment|integration)|sandbox (?:testing|integration)|"
        r"staging (?:api|environment)|conformance testing|uat\b)\b", re.IGNORECASE),
    AUDIT_COMPLIANCE: re.compile(
        r"\b(audit report|compliance checklist|cert-in|security audit|verification report|"
        r"is auditor|audit certificate|compliance certif\w*)\b", re.IGNORECASE),
    AGREEMENT_IN_PRINCIPLE: re.compile(
        r"\b(in-principle approval|in principle approval|agreement (?:signed|executed|version)|"
        r"joint undertaking|asa declaration|annexure)\b", re.IGNORECASE),
    APPLICATION_SUBMITTED: re.compile(
        r"\b(application form|onboarding application|application submitted|"
        r"appointment as (?:aua|kua)|applying (?:for|to become))\b", re.IGNORECASE),
}

_DOC_KEYWORDS: dict[str, re.Pattern] = {
    "audit_report": re.compile(r"\b(audit report|verification report|compliance checklist)\b", re.IGNORECASE),
    "in_principle_letter": re.compile(r"\b(in-?principle approval)\b", re.IGNORECASE),
    "agreement": re.compile(r"\b(aua/?kua agreement|joint undertaking|agreement (?:signed|executed))\b", re.IGNORECASE),
    "application_form": re.compile(r"\b(application form|onboarding application)\b", re.IGNORECASE),
}


def infer_stage(texts: list[str]) -> str | None:
    """Scan all of a ticket's email text (chronological order doesn't matter —
    this returns the FURTHEST stage reached, not the latest keyword seen) and
    return the highest stage whose keywords matched anywhere, or None if the
    ticket doesn't look like onboarding correspondence at all."""
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return None
    for stage in reversed(STAGES):  # furthest-first
        if _STAGE_KEYWORDS[stage].search(blob):
            return stage
    return None


def classify_document(subject: str | None, body: str | None) -> str | None:
    text = f"{subject or ''}\n{body or ''}"
    for doc_type, pattern in _DOC_KEYWORDS.items():
        if pattern.search(text):
            return doc_type
    return None


def stage_mismatch(inferred: str | None, manual: str | None) -> bool:
    if not manual or not inferred:
        return False
    return inferred != manual
