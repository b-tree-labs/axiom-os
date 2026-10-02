# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Sigstore/PEP 740 provenance verification at install time — ADR-109's second half.

AEOS §9.2 requires ``axi ext install`` to verify a Sigstore attestation against
the declared publisher identity before installing. ADR-109 (Accepted) named the
verification half unmet; these tests are the closing of that gap.

Doctrine under test (security-critical, fail-closed):

- verification is ON by default for the remote (PyPI) install source;
- every refusal names *what* failed (identity mismatch vs missing attestation
  vs network vs missing verifier dependency);
- a missing verifier dependency REFUSES — it never warn-and-continues;
- ``--no-verify`` is loud and leaves a receipt in the bypass audit log;
- local ``file://`` sources are exempt, and the exemption is stated.

All sigstore-touching calls are mocked — no network in the default run. One
integration-marked test verifies a real artifact when the verifier and the
network are available, and skips otherwise.
"""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

import pytest

from axiom.cli.ext import provenance as prov
from axiom.cli.ext.commands import install as install_mod
from axiom.cli.ext.commands.install import InstallProvider
from axiom.cli.ext.provider import CliContext

# ---------------------------------------------------------------------------
# Fixtures + stubs
# ---------------------------------------------------------------------------

EXPECTED_REPO = "b-tree-labs/axiom-os-private"
EXPECTED_WORKFLOW = "publish.yml"


def _good_provenance(
    repository: str = EXPECTED_REPO,
    workflow: str = EXPECTED_WORKFLOW,
    *,
    attestations: int = 1,
) -> dict:
    """A PEP 740 provenance object shaped like PyPI's integrity API returns."""
    return {
        "version": 1,
        "attestation_bundles": [
            {
                "publisher": {
                    "kind": "GitHub",
                    "repository": repository,
                    "workflow": workflow,
                    "environment": "pypi",
                },
                "attestations": [{"envelope": f"fake-{i}"} for i in range(attestations)],
            }
        ],
    }


def _stub_verifier(verify_raises: str | None = None) -> types.SimpleNamespace:
    """A stand-in for the ``pypi_attestations`` module.

    Mirrors the API surface :mod:`axiom.cli.ext.provenance` consumes:
    ``Distribution.from_file``, ``GitHubPublisher``, ``Attestation.model_validate``
    + ``Attestation.verify``, and ``VerificationError``. When ``verify_raises``
    is a message, ``verify()`` raises the stub's own ``VerificationError``.
    """

    class VerificationError(Exception):
        pass

    class Distribution:
        def __init__(self, path: Path) -> None:
            self.path = path

        @classmethod
        def from_file(cls, path: Path) -> Distribution:
            return cls(path)

    class GitHubPublisher:
        def __init__(
            self,
            repository: str,
            workflow: str | None = None,
            environment: str | None = None,
        ) -> None:
            self.repository = repository
            self.workflow = workflow
            self.environment = environment

    class Attestation:
        calls: list[tuple[object, object]] = []

        def __init__(self, data: dict) -> None:
            self.data = data

        @classmethod
        def model_validate(cls, data: dict) -> Attestation:
            return cls(data)

        def verify(self, identity, dist):
            Attestation.calls.append((identity, dist))
            if verify_raises is not None:
                raise VerificationError(verify_raises)
            return ("https://docs.pypi.org/attestations/publish/v1", {})

    return types.SimpleNamespace(
        VerificationError=VerificationError,
        Distribution=Distribution,
        GitHubPublisher=GitHubPublisher,
        Attestation=Attestation,
    )


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    monkeypatch.delenv("AXIOM_INSTALL_NO_PIP", raising=False)
    return home


@pytest.fixture
def fake_wheel(tmp_path: Path) -> Path:
    wheel = tmp_path / "greeter-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"not really a wheel, and it does not need to be")
    return wheel


@pytest.fixture
def expected() -> prov.ExpectedPublisher:
    return prov.ExpectedPublisher(repository=EXPECTED_REPO, workflow=EXPECTED_WORKFLOW)


def _use_verifier(monkeypatch: pytest.MonkeyPatch, stub) -> None:
    monkeypatch.setitem(sys.modules, "pypi_attestations", stub)


def _no_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``None`` in sys.modules makes ``import pypi_attestations`` raise
    # ImportError even if the package IS installed in the running venv.
    monkeypatch.setitem(sys.modules, "pypi_attestations", None)


# ---------------------------------------------------------------------------
# verify_distribution — the core seam
# ---------------------------------------------------------------------------


class TestVerifyDistribution:
    def test_verify_success(self, monkeypatch, fake_wheel, expected) -> None:
        stub = _stub_verifier()
        _use_verifier(monkeypatch, stub)
        result = prov.verify_distribution(
            fake_wheel,
            name="greeter",
            version="1.2.3",
            expected=expected,
            provenance=_good_provenance(),
        )
        assert result.repository == EXPECTED_REPO
        assert result.workflow == EXPECTED_WORKFLOW
        assert result.attestation_count == 1
        # The cryptographic verify really ran, against the pinned identity.
        (identity, dist), = stub.Attestation.calls
        assert identity.repository == EXPECTED_REPO
        assert dist.path == fake_wheel

    def test_identity_mismatch_refused(self, monkeypatch, fake_wheel, expected) -> None:
        _use_verifier(monkeypatch, _stub_verifier())
        with pytest.raises(prov.IdentityMismatch) as exc_info:
            prov.verify_distribution(
                fake_wheel,
                name="greeter",
                version="1.2.3",
                expected=expected,
                provenance=_good_provenance(repository="attacker/typosquat"),
            )
        msg = str(exc_info.value)
        # The refusal names both sides so the operator can see the mismatch.
        assert EXPECTED_REPO in msg
        assert "attacker/typosquat" in msg

    def test_workflow_mismatch_is_identity_mismatch(
        self, monkeypatch, fake_wheel, expected
    ) -> None:
        _use_verifier(monkeypatch, _stub_verifier())
        with pytest.raises(prov.IdentityMismatch):
            prov.verify_distribution(
                fake_wheel,
                name="greeter",
                version="1.2.3",
                expected=expected,
                provenance=_good_provenance(workflow="rogue.yml"),
            )

    def test_missing_attestation_refused(self, monkeypatch, fake_wheel, expected) -> None:
        _use_verifier(monkeypatch, _stub_verifier())
        with pytest.raises(prov.MissingAttestation):
            prov.verify_distribution(
                fake_wheel,
                name="greeter",
                version="1.2.3",
                expected=expected,
                provenance={"version": 1, "attestation_bundles": []},
            )

    def test_bundle_with_zero_attestations_refused(
        self, monkeypatch, fake_wheel, expected
    ) -> None:
        _use_verifier(monkeypatch, _stub_verifier())
        with pytest.raises(prov.MissingAttestation):
            prov.verify_distribution(
                fake_wheel,
                name="greeter",
                version="1.2.3",
                expected=expected,
                provenance=_good_provenance(attestations=0),
            )

    def test_cryptographic_failure_refused(self, monkeypatch, fake_wheel, expected) -> None:
        stub = _stub_verifier(verify_raises="signature does not chain to Fulcio root")
        _use_verifier(monkeypatch, stub)
        with pytest.raises(prov.AttestationInvalid) as exc_info:
            prov.verify_distribution(
                fake_wheel,
                name="greeter",
                version="1.2.3",
                expected=expected,
                provenance=_good_provenance(),
            )
        assert "Fulcio" in str(exc_info.value)

    def test_missing_verifier_dependency_refuses_even_with_provenance_in_hand(
        self, monkeypatch, fake_wheel, expected
    ) -> None:
        """Fail-closed: no verifier dep -> refusal, never warn-and-continue.

        Provenance is already fetched and handed in — the refusal must still
        happen, and must say what to install.
        """
        _no_verifier(monkeypatch)
        with pytest.raises(prov.VerifierUnavailable) as exc_info:
            prov.verify_distribution(
                fake_wheel,
                name="greeter",
                version="1.2.3",
                expected=expected,
                provenance=_good_provenance(),
            )
        assert "pypi-attestations" in str(exc_info.value)

    def test_verifier_check_precedes_network_fetch(
        self, monkeypatch, fake_wheel, expected
    ) -> None:
        """A machine without the verifier never even fetches provenance."""
        _no_verifier(monkeypatch)

        def _boom(*a, **k):  # pragma: no cover — must not be reached
            raise AssertionError("fetch_provenance called before verifier check")

        monkeypatch.setattr(prov, "fetch_provenance", _boom)
        with pytest.raises(prov.VerifierUnavailable):
            prov.verify_distribution(
                fake_wheel, name="greeter", version="1.2.3", expected=expected
            )


# ---------------------------------------------------------------------------
# fetch_provenance — 404 vs network taxonomy
# ---------------------------------------------------------------------------


class TestFetchProvenance:
    def test_404_is_missing_attestation(self, monkeypatch) -> None:
        class _Resp:
            status_code = 404

            def json(self):  # pragma: no cover
                return {}

        monkeypatch.setattr(prov.requests, "get", lambda *a, **k: _Resp())
        with pytest.raises(prov.MissingAttestation) as exc_info:
            prov.fetch_provenance("greeter", "1.2.3", "greeter-1.2.3-py3-none-any.whl")
        assert "greeter" in str(exc_info.value)

    def test_network_failure_is_network_error(self, monkeypatch) -> None:
        def _raise(*a, **k):
            raise prov.requests.ConnectionError("dns says no")

        monkeypatch.setattr(prov.requests, "get", _raise)
        with pytest.raises(prov.ProvenanceNetworkError):
            prov.fetch_provenance("greeter", "1.2.3", "greeter-1.2.3-py3-none-any.whl")

    def test_success_returns_parsed_json(self, monkeypatch) -> None:
        class _Resp:
            status_code = 200

            def json(self):
                return _good_provenance()

        monkeypatch.setattr(prov.requests, "get", lambda *a, **k: _Resp())
        data = prov.fetch_provenance("greeter", "1.2.3", "greeter-1.2.3-py3-none-any.whl")
        assert data["attestation_bundles"]


# ---------------------------------------------------------------------------
# Expected-publisher resolution
# ---------------------------------------------------------------------------


class TestExpectedPublisherResolution:
    def test_platform_distribution_has_builtin_pin(self, axiom_home) -> None:
        pub = prov.resolve_expected_publisher("axiom-os-lm")
        assert pub.repository == EXPECTED_REPO
        assert pub.workflow == EXPECTED_WORKFLOW

    def test_unknown_package_refused_with_actionable_message(self, axiom_home) -> None:
        with pytest.raises(prov.UnknownPublisher) as exc_info:
            prov.resolve_expected_publisher("some-random-package")
        msg = str(exc_info.value)
        assert "--publisher" in msg
        assert "trusted-publishers.json" in msg

    def test_pin_file_is_honored(self, axiom_home: Path) -> None:
        prov.trusted_publishers_path().write_text(
            '{"schema_version": 1, "publishers": {"greeter": '
            '{"repository": "acme/greeter", "workflow": "release.yml"}}}',
            encoding="utf-8",
        )
        pub = prov.resolve_expected_publisher("greeter")
        assert pub.repository == "acme/greeter"
        assert pub.workflow == "release.yml"

    def test_override_beats_pins(self, axiom_home) -> None:
        override = prov.parse_publisher_spec("acme/other@ci.yml")
        pub = prov.resolve_expected_publisher("axiom-os-lm", override=override)
        assert pub.repository == "acme/other"
        assert pub.workflow == "ci.yml"

    def test_parse_publisher_spec(self) -> None:
        pub = prov.parse_publisher_spec("owner/repo")
        assert pub.repository == "owner/repo"
        assert pub.workflow is None
        pub2 = prov.parse_publisher_spec("owner/repo@publish.yml")
        assert pub2.workflow == "publish.yml"
        with pytest.raises(ValueError):
            prov.parse_publisher_spec("not-a-repo-slug")


# ---------------------------------------------------------------------------
# Bypass audit log
# ---------------------------------------------------------------------------


class TestBypassLog:
    def test_bypass_is_recorded(self, axiom_home: Path) -> None:
        path = prov.record_verification_bypass(
            name="greeter", version="1.2.3", source="pypi", reason="--no-verify"
        )
        text = path.read_text(encoding="utf-8")
        assert "package=greeter" in text
        assert "version=1.2.3" in text
        assert "source=pypi" in text
        assert "reason=--no-verify" in text

    def test_bypass_log_appends(self, axiom_home: Path) -> None:
        prov.record_verification_bypass(
            name="a", version="1", source="pypi", reason="--no-verify"
        )
        path = prov.record_verification_bypass(
            name="b", version="2", source="pypi", reason="--no-verify"
        )
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# CLI wiring — axi ext install <name> --from-pypi
# ---------------------------------------------------------------------------


def _run_install_cli(*argv: str, capsys) -> tuple[int, str]:
    provider = InstallProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    ctx = CliContext(cwd=Path.cwd())
    capsys.readouterr()
    rc = provider.run(args, ctx)
    captured = capsys.readouterr()
    # Refusals go to stderr (error envelope); status rows go to stdout.
    return rc, captured.out + captured.err


@pytest.fixture
def pypi_seams(monkeypatch: pytest.MonkeyPatch, axiom_home: Path):
    """Patch the network/pip seams of the --from-pypi flow.

    Returns a recorder namespace with ``installed`` (list of paths pip
    install was asked to install) and ``downloaded`` flags.
    """
    rec = types.SimpleNamespace(installed=[], downloaded=[])

    def _fake_download(name: str, version: str, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        wheel = dest / f"{name}-{version}-py3-none-any.whl"
        wheel.write_bytes(b"fake wheel bytes")
        rec.downloaded.append(wheel)
        return wheel

    def _fake_pip_install(path: Path, *, announce) -> tuple[int, str]:
        rec.installed.append(Path(path))
        return 0, "ok"

    monkeypatch.setattr(install_mod, "_pip_download", _fake_download)
    monkeypatch.setattr(install_mod, "_pip_install", _fake_pip_install)
    monkeypatch.setattr(install_mod, "_pypi_latest_version", lambda name: "1.2.3")
    return rec


class TestInstallFromPyPI:
    def test_verified_install_happy_path(
        self, monkeypatch, pypi_seams, capsys
    ) -> None:
        _use_verifier(monkeypatch, _stub_verifier())
        monkeypatch.setattr(
            prov, "fetch_provenance", lambda *a, **k: _good_provenance()
        )
        rc, out = _run_install_cli(
            "greeter@1.2.3",
            "--from-pypi",
            "--publisher",
            f"{EXPECTED_REPO}@{EXPECTED_WORKFLOW}",
            capsys=capsys,
        )
        assert rc == 0, out
        assert "Sigstore" in out
        assert EXPECTED_REPO in out
        assert pypi_seams.installed, "pip install must run after verification"

    def test_identity_mismatch_refuses_and_never_installs(
        self, monkeypatch, pypi_seams, capsys
    ) -> None:
        _use_verifier(monkeypatch, _stub_verifier())
        monkeypatch.setattr(
            prov,
            "fetch_provenance",
            lambda *a, **k: _good_provenance(repository="attacker/typosquat"),
        )
        rc, out = _run_install_cli(
            "greeter@1.2.3",
            "--from-pypi",
            "--publisher",
            f"{EXPECTED_REPO}@{EXPECTED_WORKFLOW}",
            capsys=capsys,
        )
        assert rc == 1
        assert "identity" in out.lower()
        assert "attacker/typosquat" in out
        assert not pypi_seams.installed, "refusal must abort before pip install"

    def test_missing_attestation_refuses_and_never_installs(
        self, monkeypatch, pypi_seams, capsys
    ) -> None:
        _use_verifier(monkeypatch, _stub_verifier())
        monkeypatch.setattr(
            prov,
            "fetch_provenance",
            lambda *a, **k: (_ for _ in ()).throw(
                prov.MissingAttestation(
                    "no PEP 740 provenance published for greeter 1.2.3"
                )
            ),
        )
        rc, out = _run_install_cli(
            "greeter@1.2.3",
            "--from-pypi",
            "--publisher",
            EXPECTED_REPO,
            capsys=capsys,
        )
        assert rc == 1
        assert "attestation" in out.lower()
        assert not pypi_seams.installed

    def test_missing_verifier_dependency_refuses_remote_install(
        self, monkeypatch, pypi_seams, capsys
    ) -> None:
        """Fail-closed: no pypi-attestations -> the remote install refuses.

        The refusal happens before anything is downloaded, and names the
        dependency to install.
        """
        _no_verifier(monkeypatch)
        rc, out = _run_install_cli(
            "greeter@1.2.3",
            "--from-pypi",
            "--publisher",
            EXPECTED_REPO,
            capsys=capsys,
        )
        assert rc == 1
        assert "pypi-attestations" in out
        assert not pypi_seams.downloaded, "must refuse before downloading"
        assert not pypi_seams.installed

    def test_unknown_publisher_refuses(self, monkeypatch, pypi_seams, capsys) -> None:
        _use_verifier(monkeypatch, _stub_verifier())
        rc, out = _run_install_cli(
            "greeter@1.2.3", "--from-pypi", capsys=capsys
        )
        assert rc == 1
        assert "--publisher" in out
        assert not pypi_seams.installed

    def test_no_verify_is_loud_and_recorded(
        self, monkeypatch, pypi_seams, axiom_home: Path, capsys
    ) -> None:
        # No verifier stub on purpose: --no-verify must not need one.
        _no_verifier(monkeypatch)
        rc, out = _run_install_cli(
            "greeter@1.2.3", "--from-pypi", "--no-verify", capsys=capsys
        )
        assert rc == 0, out
        # Loud: a WARN row names the skipped verification.
        assert "[WARN]" in out
        assert "UNVERIFIED" in out
        # Recorded: the bypass landed in the audit log.
        log = prov.bypass_log_path()
        assert log.exists()
        text = log.read_text(encoding="utf-8")
        assert "package=greeter" in text
        assert "source=pypi" in text
        # And the output tells the operator where the receipt lives.
        assert log.name in out

    def test_no_verify_still_installs(self, monkeypatch, pypi_seams, capsys) -> None:
        _no_verifier(monkeypatch)
        rc, _ = _run_install_cli(
            "greeter@1.2.3", "--from-pypi", "--no-verify", capsys=capsys
        )
        assert rc == 0
        assert pypi_seams.installed


# ---------------------------------------------------------------------------
# Local sources stay exempt — and say so
# ---------------------------------------------------------------------------


class TestLocalExemption:
    def test_registry_install_states_sigstore_exemption(
        self, scaffolded_extension, axiom_home: Path, monkeypatch, capsys
    ) -> None:
        from axiom.cli.ext.commands.publish import publish_extension

        monkeypatch.setenv("AXIOM_INSTALL_NO_PIP", "1")
        ext = scaffolded_extension("greeter")
        publish_extension(ext, yes=True, skip_tag_check=True)
        rc, out = _run_install_cli("greeter", "--no-pip", capsys=capsys)
        assert rc == 0, out
        assert "exempt" in out.lower()
        assert "ed25519" in out.lower()


# ---------------------------------------------------------------------------
# Integration — real verification, when locally possible
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_real_provenance_of_platform_release() -> None:
    """Verify a real PyPI provenance document end-to-end (identity layer).

    Requires network. Cryptographic verification additionally requires the
    ``pypi-attestations`` package; when it is absent we still assert the
    identity layer against the live provenance document, which is the part
    this repo controls.
    """
    name, version, filename = (
        "axiom-os-lm",
        "0.56.0",
        "axiom_os_lm-0.56.0-py3-none-any.whl",
    )
    try:
        provenance = prov.fetch_provenance(name, version, filename)
    except prov.ProvenanceNetworkError as exc:
        pytest.skip(f"network unavailable: {exc}")
    expected = prov.resolve_expected_publisher(name)
    bundles = prov.matching_bundles(provenance, expected)
    assert bundles, "live provenance no longer matches the pinned identity"
    try:
        import pypi_attestations  # noqa: F401
    except ImportError:
        pytest.skip("pypi-attestations not installed; identity layer verified only")
    # Full cryptographic path: download the artifact, then verify.
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        dl = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--no-deps",
                "--only-binary",
                ":all:",
                "--dest",
                td,
                f"{name}=={version}",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if dl.returncode != 0:
            pytest.skip(f"pip download failed: {dl.stderr[-300:]}")
        wheel = next(Path(td).glob("*.whl"))
        result = prov.verify_distribution(
            wheel, name=name, version=version, expected=expected, provenance=provenance
        )
        assert result.attestation_count >= 1
