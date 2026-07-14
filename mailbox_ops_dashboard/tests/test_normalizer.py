"""Unit tests for the email normalizer (the connector output contract)."""
from ingestion.email_normalizer import (
    NORMALIZED_FIELDS,
    make_snippet,
    normalize_from_string,
    normalize_subject,
)

RAW = """From: Ramesh Kumar <ramesh.kumar@example.com>
To: UIDAI Helpdesk <helpdesk@uidai.gov.in>
Cc: Supervisor <sup@uidai.gov.in>
Subject: Re: [Ticket] Unable to update address
Date: Mon, 06 Jul 2026 09:15:00 +0530
Message-ID: <req-0001@example.com>
In-Reply-To: <prev@example.com>
References: <root@example.com> <prev@example.com>
Content-Type: text/plain; charset="utf-8"

Hello, this is the body.
Regards,
Ramesh
"""


def test_normalize_subject_strips_prefixes_and_tags():
    assert normalize_subject("Re: [Ticket] Unable to update address") == "unable to update address"
    assert normalize_subject("FWD: Fw: Hello  World") == "hello world"
    assert normalize_subject("") == ""


def test_normalize_produces_full_contract():
    n = normalize_from_string(RAW, provider="eml_export", ingestion_mode="export", folder="export")
    for field in NORMALIZED_FIELDS:
        assert field in n, f"missing {field}"
    assert n["from_email"] == "ramesh.kumar@example.com"
    assert n["from_name"] == "Ramesh Kumar"
    assert n["to_emails"] == ["helpdesk@uidai.gov.in"]
    assert n["cc_emails"] == ["sup@uidai.gov.in"]
    assert n["internet_message_id"] == "<req-0001@example.com>"
    assert n["in_reply_to"] == "<prev@example.com>"
    assert "<root@example.com>" in n["references"]
    assert n["normalized_subject"] == "unable to update address"
    assert n["ingestion_mode"] == "export"
    assert "body" in n["body_text"].lower()


def test_fallback_message_id_when_missing():
    raw_no_id = RAW.replace("Message-ID: <req-0001@example.com>\n", "")
    n = normalize_from_string(raw_no_id, provider="eml_export", ingestion_mode="export")
    assert n["internet_message_id"].startswith("<gen-")


def test_snippet_truncates():
    assert make_snippet("x" * 500, limit=100).endswith("…")
    assert len(make_snippet("short")) == len("short")
