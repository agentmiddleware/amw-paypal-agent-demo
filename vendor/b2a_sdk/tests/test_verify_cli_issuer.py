"""Key-set fetch in verify_cli only uses https, except loopback http."""

import httpx
import pytest

from b2a_sdk.verify_cli import (
    EXIT_UNDETERMINED,
    VerificationError,
    _check_issuer_origin,
    _fetch_key_document,
    main,
)


@pytest.mark.parametrize(
    "issuer",
    [
        "http://api.example.com",
        "http://api.example.com/",
        "http://api.example.com:80",
        "http://api.example.com/.well-known/trust-keys.json",
        "http://192.168.1.10",
        "ftp://api.example.com",
    ],
)
def test_remote_cleartext_issuers_are_refused(issuer):
    """Remote key fetches without https fail closed before any network use."""
    with pytest.raises(VerificationError):
        _check_issuer_origin(issuer)


def test_refused_fetch_sends_no_request(monkeypatch):
    """The refusal happens before httpx is even called."""

    def fail(url, timeout=None):
        raise AssertionError("no request should be sent")

    monkeypatch.setattr(httpx, "get", fail)
    with pytest.raises(VerificationError):
        _fetch_key_document("http://api.example.com", 10.0)


@pytest.mark.parametrize(
    ("issuer", "expected"),
    [
        ("https://api.example.com", "https://api.example.com/.well-known/trust-keys.json"),
        ("https://api.example.com/", "https://api.example.com/.well-known/trust-keys.json"),
        ("api.example.com", "https://api.example.com/.well-known/trust-keys.json"),
        ("HTTPS://API.EXAMPLE.COM", "https://api.example.com/.well-known/trust-keys.json"),
        (
            "https://api.example.com:8443",
            "https://api.example.com:8443/.well-known/trust-keys.json",
        ),
        ("http://localhost", "http://localhost/.well-known/trust-keys.json"),
        ("http://localhost:8000", "http://localhost:8000/.well-known/trust-keys.json"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000/.well-known/trust-keys.json"),
        ("http://[::1]:8000", "http://[::1]:8000/.well-known/trust-keys.json"),
    ],
)
def test_allowed_issuers_map_to_exact_key_url(issuer, expected):
    assert _check_issuer_origin(issuer) == expected


def test_localhost_http_fetch_uses_http_url(monkeypatch):
    """Loopback http still works, for local development and tests."""
    seen = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"keys": []}

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: seen.append(url) or FakeResponse())
    doc = _fetch_key_document("http://localhost:8000", 10.0)

    assert doc == {"keys": []}
    assert seen == ["http://localhost:8000/.well-known/trust-keys.json"]


def test_main_reports_undetermined_for_http_issuer(tmp_path, monkeypatch):
    """main() exits 2 for an http issuer and sends no key request."""
    bundle = tmp_path / "bundle.json"
    bundle.write_text("{}", encoding="utf-8")

    def fail(url, timeout=None):
        raise AssertionError("no request should be sent")

    monkeypatch.setattr(httpx, "get", fail)
    exit_code = main(["--bundle", str(bundle), "--issuer", "http://api.example.com"])

    assert exit_code == EXIT_UNDETERMINED
