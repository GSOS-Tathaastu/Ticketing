"""Unit tests for the agent detection engine cascade."""
from tickets.agent_detection_engine import (
    HIGH,
    LOW,
    UNKNOWN,
    UNKNOWN_AGENT_LABEL,
    detect_agent,
    resolve_direction,
)


def test_inbound_from_requester_is_not_an_agent():
    d = detect_agent({"from_email": "ramesh@example.com", "folder": "inbox"})
    assert d.source == "unknown"
    assert d.email is None


def test_common_mailbox_only_is_unknown_internal_agent():
    d = detect_agent({"from_email": "helpdesk@uidai.gov.in", "folder": "sent"})
    assert d.name == UNKNOWN_AGENT_LABEL
    assert d.confidence == LOW
    assert d.source == "sent_folder_common_mailbox"


def test_known_user_match_is_high_confidence():
    class U:
        name = "Agent Rao"

    known = {"agent.rao@uidai.gov.in": U()}
    d = detect_agent({"from_email": "helpdesk@uidai.gov.in",
                      "sender_email": "agent.rao@uidai.gov.in", "folder": "sent"}, known)
    assert d.email == "agent.rao@uidai.gov.in"
    assert d.confidence == HIGH


def test_sender_header_internal_distinct_is_high():
    d = detect_agent({"from_email": "helpdesk@uidai.gov.in",
                      "sender_email": "someone@uidai.gov.in", "folder": "sent"})
    assert d.email == "someone@uidai.gov.in"
    assert d.confidence == HIGH


def test_signature_email_low_confidence():
    body = "Please re-upload.\n\nRegards,\nAgent Rao\nagent.rao@uidai.gov.in"
    d = detect_agent({"from_email": "helpdesk@uidai.gov.in", "folder": "sent", "body_text": body})
    assert d.email == "agent.rao@uidai.gov.in"
    assert d.confidence == LOW
    assert d.source == "signature"


def test_resolve_direction():
    assert resolve_direction("ramesh@example.com") == "inbound"
    assert resolve_direction("helpdesk@uidai.gov.in") == "outbound"
    assert resolve_direction("x@uidai.gov.in", folder="sent") == "outbound"
    assert resolve_direction(None) == "unknown"
