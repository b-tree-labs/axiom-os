# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every published artifact must carry a signed provenance attestation.

ADR-109. We publish through Trusted Publishing, and `pypa/gh-action-pypi-publish`
mints PEP 740 attestations signed by Sigstore's keyless flow — a short-lived
Fulcio certificate against the workflow's OIDC identity, logged to Rekor. The
result binds each wheel to a repository, a workflow and a ref, with no signing
key to leak.

**Why this file exists.** That guarantee arrived as a third-party default and was
written down nowhere: no ADR, no test, and a publish step that was a bare `uses:`
with no `with:` block. Removing it costs nothing and breaks nothing visible —
set `attestations: false`, drop `id-token: write`, or swap in a `twine upload`,
and the release still publishes, CI stays green, and the wheels silently stop
being attested. Nobody learns otherwise until someone asks us to prove where a
wheel came from.

That is a check that cannot fail, so these assertions are the check. Each one
names a distinct way the guarantee can be lost, because a single assertion on
the action name would pass while attestations were explicitly disabled.

The workflow is PARSED, not grepped. A commented-out `attestations: true`
satisfies a substring search and publishes nothing signed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML parses the workflow")

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "publish.yml"
PUBLISH_ACTION = "pypa/gh-action-pypi-publish"


def _workflow() -> dict[str, Any]:
    assert WORKFLOW.is_file(), f"publish workflow missing at {WORKFLOW}"
    return yaml.safe_load(WORKFLOW.read_text())


def _publish_job() -> dict[str, Any]:
    jobs = _workflow().get("jobs") or {}
    for name, job in jobs.items():
        for step in job.get("steps") or []:
            if PUBLISH_ACTION in str(step.get("uses", "")):
                return job
    raise AssertionError(
        f"no job in {WORKFLOW.name} publishes via {PUBLISH_ACTION}. If publishing "
        "moved to another action, provenance moved with it — see ADR-109 before "
        "deleting this test."
    )


def _publish_step() -> dict[str, Any]:
    for step in _publish_job().get("steps") or []:
        if PUBLISH_ACTION in str(step.get("uses", "")):
            return step
    raise AssertionError("unreachable: job matched but step did not")


class TestPublishedArtifactsAreAttested:
    def test_publishing_goes_through_the_attesting_action(self) -> None:
        """A `twine upload` publishes fine and signs nothing."""
        assert PUBLISH_ACTION in str(_publish_step().get("uses", ""))

    def test_attestations_are_stated_not_inherited(self) -> None:
        """ADR-109 D3: the default is written down, so removing it is deliberate.

        Asserting only that the action is used would pass with
        `attestations: false` sitting right beneath it.
        """
        with_block = _publish_step().get("with") or {}
        assert "attestations" in with_block, (
            "the publish step does not state `attestations`. It may still be "
            "attesting by default today, but nothing here says we intend it, so "
            "a future default change would remove the guarantee silently."
        )
        assert with_block["attestations"] is True, (
            f"attestations are explicitly set to {with_block['attestations']!r}. "
            "Publishing unattested artifacts is a defect under ADR-109."
        )

    def test_the_job_can_mint_an_oidc_token(self) -> None:
        """Without `id-token: write` there is no identity to sign against, and
        Trusted Publishing itself stops working."""
        perms = _publish_job().get("permissions") or {}
        assert perms.get("id-token") == "write", (
            f"publish job permissions are {perms!r}. Keyless signing needs an "
            "OIDC token; without it there is no Fulcio certificate and no "
            "attestation."
        )

    def test_no_api_token_is_configured(self) -> None:
        """Keyless means keyless. A password/token on the publish step means we
        fell back to a long-lived credential, which is the thing ADR-109 D2 says
        we do not hold."""
        with_block = _publish_step().get("with") or {}
        for key in ("password", "user", "api-token", "token"):
            assert key not in with_block, (
                f"publish step sets {key!r} — that is credential-based "
                "publishing. ADR-109 D2 keeps this keyless."
            )

    def test_the_adr_this_enforces_still_exists(self) -> None:
        """A test whose rationale has been deleted is cargo. If ADR-109 is
        superseded, this file should be revisited in the same change."""
        repo = WORKFLOW.parents[2]  # <repo>/.github/workflows/publish.yml
        adr = repo / "docs" / "adrs" / "adr-109-build-provenance-attestation.md"
        assert adr.is_file(), f"ADR-109 missing at {adr}"
        assert "attestations" in adr.read_text()
