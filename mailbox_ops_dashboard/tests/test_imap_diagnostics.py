"""The NIC/IMAP diagnostic must fail fast and cleanly (no exception) at the
first unavailable step, without ever printing the password. Only the
config-check and DNS-failure branches are exercised here — TCP/TLS/LOGIN
need a real reachable server, which is exactly what this tool is for and
isn't available in CI."""
from config.settings import settings
from connectors.imap_diagnostics import run_diagnostics


def test_missing_config_fails_fast(monkeypatch):
    monkeypatch.setattr(settings, "imap_host", "")
    monkeypatch.setattr(settings, "imap_user", "")
    monkeypatch.setattr(settings, "imap_password", "")
    steps = run_diagnostics()
    assert len(steps) == 1
    assert steps[0].name == "Config"
    assert steps[0].ok is False


def test_bad_host_fails_at_dns_step(monkeypatch):
    monkeypatch.setattr(settings, "imap_host", "nonexistent.invalid.test")
    monkeypatch.setattr(settings, "imap_user", "someone")
    monkeypatch.setattr(settings, "imap_password", "secret-value")
    steps = run_diagnostics()
    names = [s.name for s in steps]
    assert names == ["Config", "DNS resolution"]
    assert steps[0].ok is True
    assert steps[1].ok is False
    # never leak the password anywhere in the diagnostic output
    assert all("secret-value" not in s.detail for s in steps)
