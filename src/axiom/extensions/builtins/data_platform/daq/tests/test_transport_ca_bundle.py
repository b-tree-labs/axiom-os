# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``UrllibTransport`` trust configuration.

A producer at one institution pushing to another's ingest face is the normal
case, and that face is often behind an institutional or private CA. The
transport must be able to trust it explicitly — and must never offer to skip
verification instead.
"""

from __future__ import annotations

import ssl
import subprocess

import pytest

from axiom.extensions.builtins.data_platform.daq.transmitter import UrllibTransport


@pytest.fixture(scope="module")
def self_signed_pem(tmp_path_factory) -> str:
    """A real self-signed PEM — the shape a host site hands a partner."""
    d = tmp_path_factory.mktemp("ca")
    key, cert = d / "k.pem", d / "c.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=face.example",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert)


def test_default_transport_verifies_and_has_no_opener() -> None:
    t = UrllibTransport()
    assert t.ca_bundle is None
    assert t._opener is None  # falls through to urlopen's verified default


def test_ca_bundle_builds_a_verifying_opener(self_signed_pem) -> None:
    t = UrllibTransport(ca_bundle=self_signed_pem)
    assert t._opener is not None
    handler = next(
        h for h in t._opener.handlers if h.__class__.__name__ == "HTTPSHandler"
    )
    context = handler._context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_ca_bundle_actually_loads_the_certificate(self_signed_pem) -> None:
    """Not just accepted — present in the trust store the opener will use."""
    t = UrllibTransport(ca_bundle=self_signed_pem)
    handler = next(
        h for h in t._opener.handlers if h.__class__.__name__ == "HTTPSHandler"
    )
    subjects = [c["subject"] for c in handler._context.get_ca_certs()]
    assert any("face.example" in str(s) for s in subjects)


def test_missing_ca_bundle_fails_at_construction(tmp_path) -> None:
    """Loudly, at wiring time — not on the first push at 3am."""
    with pytest.raises(ValueError, match="does not exist"):
        UrllibTransport(ca_bundle=str(tmp_path / "absent.pem"))


def test_unusable_ca_bundle_fails_at_construction(tmp_path) -> None:
    junk = tmp_path / "junk.pem"
    junk.write_text("this is not a certificate\n")
    with pytest.raises(ValueError, match="not a usable PEM"):
        UrllibTransport(ca_bundle=str(junk))


def test_tilde_is_expanded(self_signed_pem, monkeypatch, tmp_path) -> None:
    import shutil

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    shutil.copy(self_signed_pem, tmp_path / "site-ca.pem")
    t = UrllibTransport(ca_bundle="~/site-ca.pem")
    assert t.ca_bundle == str(tmp_path / "site-ca.pem")


def test_there_is_no_way_to_disable_verification() -> None:
    """The guard: an 'insecure' escape hatch must never be added quietly."""
    import inspect

    signature = inspect.signature(UrllibTransport.__init__)
    banned = {"verify", "insecure", "skip_verify", "no_verify", "ssl_context"}
    assert banned.isdisjoint(signature.parameters)
    source = inspect.getsource(UrllibTransport)
    assert "CERT_NONE" not in source
    assert "_create_unverified" not in source
