# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The node chart, its image and the offline bundle are release artifacts.

ADR-109 D1: every artifact published from this repository is attested, by the
keyless flow (D2), and the guarantee is stated where a reviewer sees it (D3).
The wheel had that; the chart, the runtime image and the offline bundle did
not exist as release artifacts at all. These assertions name the ways the
guarantee can be lost for them: a job that publishes without signing, one that
signs with a key, one that can run on a pull request, and one whose failure
would stop the wheel from publishing.

The workflows are parsed, not searched: a commented-out step satisfies a
substring search and publishes nothing signed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML parses the workflows")

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
TAG_ONLY = "startsWith(github.ref, 'refs/tags/v')"
#: Commands that make something public or permanent.
PUBLISHES = ("helm push", "docker push", "cosign sign", "gh release upload", "gh release create")


def workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(wf: dict[str, Any]) -> dict[str, Any]:
    on = wf.get("on", wf.get(True))  # YAML reads a bare `on` as True
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on or {}


def script(job: dict[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps") or [])


def job(name: str) -> dict[str, Any]:
    jobs = workflow("publish.yml")["jobs"]
    assert name in jobs, f"publish.yml has no `{name}` job"
    return jobs[name]


@pytest.mark.parametrize("name", ["node_artifacts", "offline_bundle"])
class TestEachReleaseJob:
    def test_it_runs_only_for_a_version_tag(self, name):
        assert TAG_ONLY in str(job(name).get("if", "")), (
            f"`{name}` must be gated on a version tag: publish.yml can also be "
            "started by hand from any ref"
        )

    def test_it_waits_only_for_jobs_that_exist(self, name):
        """A job that needs a missing job never runs, and nothing goes red."""
        needs = job(name).get("needs") or []
        assert needs and set(needs) <= set(workflow("publish.yml")["jobs"]), needs

    def test_it_can_ask_for_an_identity_to_sign_with(self, name):
        assert job(name).get("permissions", {}).get("id-token") == "write"

    def test_it_signs_keylessly(self, name):
        """ADR-109 D2: there is no signing key to hold, leak or rotate."""
        text = script(job(name))
        assert "cosign sign" in text
        assert "--key" not in text and "COSIGN_PRIVATE_KEY" not in str(job(name))

    def test_the_wheel_does_not_wait_for_it(self, name):
        """A registry or signer outage must not stop the package release."""
        jobs = workflow("publish.yml")["jobs"]
        for other, body in jobs.items():
            needs = body.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            if other not in ("node_artifacts", "offline_bundle"):
                assert name not in needs, f"`{other}` waits for `{name}`"

    def test_it_installs_the_signer_by_checksum(self, name):
        """The version and digest are the declaration's, the ones the bundle ships."""
        text = script(job(name))
        assert "sha256sum -c" in text and "offline_bundle.py plan" in text


def test_the_chart_and_the_image_are_signed_by_digest():
    text = script(job("node_artifacts"))
    assert job("node_artifacts")["env"]["REGISTRY"].startswith("ghcr.io/")
    assert "helm push" in text and "oci://$REGISTRY/" in text
    # A tag can be moved after signing; a digest cannot.
    for line in text.splitlines():
        if "cosign sign " in line and "sign-blob" not in line:
            assert "@" in line and ":$" not in line.split("cosign sign", 1)[1], line


def test_the_chart_is_versioned_with_the_release_and_pinned_to_the_image_digest():
    text = script(job("node_artifacts"))
    assert "helm package" in text and "--version" in text
    assert "digest" in text


def test_the_bundle_manifest_is_signed_and_the_signature_checked_before_upload():
    text = script(job("offline_bundle"))
    sign = text.index("cosign sign-blob")
    verify = text.index("install-from-bundle.py\" verify")
    upload = text.index("gh release upload")
    assert sign < verify < upload
    assert "--allow-unsigned" not in text[verify:upload]


def test_the_bundle_architecture_is_a_parameter():
    matrix = job("offline_bundle")["strategy"]["matrix"]
    assert "arch" in matrix
    assert "AXIOM_OFFLINE_BUNDLE_ARCHES" in str(matrix["arch"]) and "amd64" in str(matrix["arch"])
    assert "--arch" in script(job("offline_bundle"))


def test_nothing_is_published_from_a_pull_request():
    """Every publishing job in a workflow a pull request can start is gated
    on a tag or on a push to main."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        wf = yaml.safe_load(path.read_text())
        if not {"pull_request", "pull_request_target", "merge_group"} & set(triggers(wf)):
            continue
        for name, body in (wf.get("jobs") or {}).items():
            text = script(body)
            if not any(p in text for p in PUBLISHES):
                continue
            gate = str(body.get("if", ""))
            assert "refs/tags/" in gate or "github.event_name == 'push'" in gate, (
                f"{path.name}: `{name}` publishes and a pull request can start it"
            )
