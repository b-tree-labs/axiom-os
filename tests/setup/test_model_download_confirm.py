# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A multi-GB model download must state its size and ask first.

Sandbox audit 2026-09-18, gap 2: behind a default-Yes infra prompt the
wizard downloaded the 4.7GB default GGUF immediately, with no size
confirmation between the Enter keypress and the download. The confirm gate
must default to No, name the size, and point at the smaller profile.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from axiom.setup.renderer import set_color_enabled
from axiom.setup.state import SetupState
from axiom.setup.wizard import SetupWizard


@pytest.fixture(autouse=True)
def _disable_color():
    set_color_enabled(False)
    yield
    set_color_enabled(False)


def _bare_wizard() -> SetupWizard:
    wizard = SetupWizard.__new__(SetupWizard)
    wizard.root = None
    wizard.state = SetupState()
    wizard.probe_result = None
    wizard.requested_model = None
    wizard._resolved_model = None
    return wizard


class TestConfirmModelDownload:
    def test_already_downloaded_model_needs_no_prompt(self):
        wizard = _bare_wizard()
        with (
            patch("axiom.setup.llamafile.is_llamafile_installed", return_value=True),
            patch("axiom.setup.renderer.prompt_yn") as prompt,
        ):
            assert wizard._confirm_model_download("qwen") is True
        prompt.assert_not_called()

    def test_prompt_states_size_and_defaults_to_no(self, capsys):
        wizard = _bare_wizard()
        with (
            patch("axiom.setup.llamafile.is_llamafile_installed", return_value=False),
            patch("axiom.setup.renderer.prompt_yn", return_value=False) as prompt,
        ):
            result = wizard._confirm_model_download("qwen")

        assert result is False
        # Explicit confirm, never default-Yes on a multi-GB pull.
        assert prompt.call_args.kwargs.get("default") is False
        # The question itself carries the size.
        question = prompt.call_args.args[0]
        assert "4.7" in question

    def test_prompt_advertises_small_profile_and_dry_run(self, capsys):
        wizard = _bare_wizard()
        with (
            patch("axiom.setup.llamafile.is_llamafile_installed", return_value=False),
            patch("axiom.setup.renderer.prompt_yn", return_value=False),
        ):
            wizard._confirm_model_download("qwen")

        out = capsys.readouterr().out
        assert "--model small" in out
        assert "1.6" in out
        assert "--dry-run" in out

    def test_accepting_returns_true(self):
        wizard = _bare_wizard()
        with (
            patch("axiom.setup.llamafile.is_llamafile_installed", return_value=False),
            patch("axiom.setup.renderer.prompt_yn", return_value=True),
        ):
            assert wizard._confirm_model_download("qwen") is True

    def test_declining_names_the_later_command(self, capsys):
        wizard = _bare_wizard()
        with (
            patch("axiom.setup.llamafile.is_llamafile_installed", return_value=False),
            patch("axiom.setup.renderer.prompt_yn", return_value=False),
        ):
            wizard._confirm_model_download("qwen")

        out = capsys.readouterr().out
        # Every skipped component names the command that sets it up later.
        assert "config --model" in out


class TestProvisionInfrastructureLLMGate:
    """provision_infrastructure must honor provision_llm=False (skip the
    llamafile download entirely) on the non-K3D paths."""

    def test_compose_path_skips_llm_when_declined(self):
        with (
            patch("axiom.setup.infra.detect_infra_path", return_value="docker-compose"),
            patch("axiom.setup.infra.provision_postgres_compose", return_value=True) as pg,
            patch("axiom.setup.llamafile.provision") as llm,
        ):
            from axiom.setup.infra import provision_infrastructure

            path = provision_infrastructure(model="qwen", provision_llm=False)

        assert path == "docker-compose"
        pg.assert_called_once()
        llm.assert_not_called()

    def test_compose_path_provisions_llm_by_default(self):
        with (
            patch("axiom.setup.infra.detect_infra_path", return_value="docker-compose"),
            patch("axiom.setup.infra.provision_postgres_compose", return_value=True),
            patch("axiom.setup.llamafile.provision") as llm,
        ):
            from axiom.setup.infra import provision_infrastructure

            provision_infrastructure(model="qwen")

        llm.assert_called_once()

    def test_native_path_skips_llm_when_declined(self):
        with (
            patch("axiom.setup.infra.detect_infra_path", return_value="native"),
            patch(
                "axiom.setup.infra.provision_postgres_native",
                return_value={"running": True, "method": "existing"},
            ),
            patch("axiom.setup.llamafile.provision") as llm,
        ):
            from axiom.setup.infra import provision_infrastructure

            path = provision_infrastructure(model="qwen", provision_llm=False)

        assert path == "native"
        llm.assert_not_called()


class TestPhaseInfraWiresTheGate:
    """_phase_infra passes the confirm-gate result into provision_infrastructure."""

    def _run_phase(self, confirm: bool) -> MagicMock:
        wizard = _bare_wizard()
        provision = MagicMock(return_value="docker-compose")
        infra_status = MagicMock()
        infra_status.status = "missing"  # not READY → skips the K3D-ready path
        with (
            patch("axiom.setup.infra.check_docker", return_value=infra_status),
            patch("axiom.setup.infra.check_k3d", return_value=infra_status),
            patch("axiom.setup.infra.detect_infra_path", return_value="docker-compose"),
            patch("axiom.setup.infra.provision_infrastructure", provision),
            patch("axiom.setup.infra.run_infra_setup"),
            patch("axiom.setup.renderer.prompt_yn", return_value=True),
            patch.object(wizard, "resolve_local_model", return_value="qwen"),
            patch.object(wizard, "_confirm_model_download", return_value=confirm),
        ):
            wizard._phase_infra()
        return provision

    def test_declined_download_provisions_without_llm(self):
        provision = self._run_phase(confirm=False)
        assert provision.call_args.kwargs.get("provision_llm") is False

    def test_accepted_download_provisions_with_llm(self):
        provision = self._run_phase(confirm=True)
        assert provision.call_args.kwargs.get("provision_llm") is True
