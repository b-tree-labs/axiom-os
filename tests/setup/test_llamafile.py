# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for llamafile provisioning and infra path detection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Model profile tests
# ---------------------------------------------------------------------------


class TestModelProfiles:
    def test_default_model_is_qwen(self):
        from axiom.setup.llamafile import (
            DEFAULT_LOCAL_MODEL_GGUF,
            DEFAULT_LOCAL_MODEL_ID,
            DEFAULT_MODEL,
        )

        assert DEFAULT_MODEL == "qwen"
        assert DEFAULT_LOCAL_MODEL_GGUF == "qwen2.5-7b-instruct-q4_k_m.gguf"
        assert DEFAULT_LOCAL_MODEL_ID == "qwen2.5-7b-instruct"

    def test_resolve_model_qwen(self):
        from axiom.setup.llamafile import resolve_model

        profile = resolve_model("qwen")
        assert profile["gguf"] == "qwen2.5-7b-instruct-q4_k_m.gguf"
        assert profile["id"] == "qwen2.5-7b-instruct"
        assert "qwen2.5-7b-instruct" in profile["url"].lower()
        assert profile["url"].endswith(".gguf")
        assert profile["size_gb"] == 4.7

    def test_resolve_model_bonsai(self):
        from axiom.setup.llamafile import resolve_model

        profile = resolve_model("bonsai")
        assert profile["gguf"] == "Bonsai-1.7B.gguf"
        assert profile["id"] == "bonsai-1.7b"
        assert "Bonsai" in profile["url"]
        assert profile["url"].endswith(".gguf")

    def test_resolve_model_unknown_raises(self):
        from axiom.setup.llamafile import resolve_model

        with pytest.raises(KeyError) as exc_info:
            resolve_model("gpt5")
        # Error message must list valid names
        msg = str(exc_info.value)
        assert "qwen" in msg
        assert "bonsai" in msg


# ---------------------------------------------------------------------------
# llamafile module tests
# ---------------------------------------------------------------------------


class TestGetLlamafileDir:
    def test_creates_directory(self, tmp_path):
        with patch("axiom.setup.llamafile.Path") as mock_path:
            mock_dir = MagicMock()
            mock_path.home.return_value.__truediv__ = MagicMock(return_value=mock_dir)
            mock_dir.__truediv__ = MagicMock(return_value=mock_dir)
            mock_dir.mkdir = MagicMock()

            from axiom.setup.llamafile import get_llamafile_dir

            # Reset the mock to use tmp_path for a real test
        with patch("axiom.setup.llamafile.Path.home", return_value=tmp_path):
            from axiom.setup.llamafile import get_llamafile_dir

            result = get_llamafile_dir()
            assert result.exists()
            assert result == tmp_path / ".axi" / "llamafile"


class TestIsLlamafileInstalled:
    def test_returns_false_when_not_downloaded(self, tmp_path):
        with patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path):
            from axiom.setup.llamafile import is_llamafile_installed

            assert is_llamafile_installed() is False

    def test_returns_true_when_qwen_default_present(self, tmp_path):
        (tmp_path / "llamafile").touch()
        (tmp_path / "qwen2.5-7b-instruct-q4_k_m.gguf").touch()

        with patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path):
            from axiom.setup.llamafile import is_llamafile_installed

            assert is_llamafile_installed() is True

    def test_returns_false_when_only_binary(self, tmp_path):
        (tmp_path / "llamafile").touch()

        with patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path):
            from axiom.setup.llamafile import is_llamafile_installed

            assert is_llamafile_installed() is False

    def test_qwen_cached_only_returns_true_for_qwen_profile(self, tmp_path):
        # Qwen present, Bonsai missing — explicit qwen check should pass
        (tmp_path / "llamafile").touch()
        (tmp_path / "qwen2.5-7b-instruct-q4_k_m.gguf").touch()

        with patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path):
            from axiom.setup.llamafile import is_llamafile_installed

            assert is_llamafile_installed(model="qwen") is True
            assert is_llamafile_installed(model="bonsai") is False

    def test_bonsai_cached_only_returns_true_for_bonsai_profile(self, tmp_path):
        # Bonsai present, qwen missing — explicit bonsai check should pass
        (tmp_path / "llamafile").touch()
        (tmp_path / "Bonsai-1.7B.gguf").touch()

        with patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path):
            from axiom.setup.llamafile import is_llamafile_installed

            assert is_llamafile_installed(model="bonsai") is True
            assert is_llamafile_installed(model="qwen") is False


class TestDetectExistingBonsaiCache:
    def test_returns_none_when_missing(self, tmp_path):
        with patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path):
            from axiom.setup.llamafile import detect_existing_bonsai_cache

            assert detect_existing_bonsai_cache() is None

    def test_returns_path_when_present(self, tmp_path):
        cached = tmp_path / "Bonsai-1.7B.gguf"
        cached.touch()
        with patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path):
            from axiom.setup.llamafile import detect_existing_bonsai_cache

            result = detect_existing_bonsai_cache()
            assert result == cached
            assert result.exists()


class TestDownloadModel:
    @staticmethod
    def _mock_requests():
        mock_resp = MagicMock()
        mock_resp.headers = {"content-length": "100"}
        mock_resp.iter_content.return_value = [b"x" * 100]
        mock_resp.raise_for_status = MagicMock()
        mock_requests = MagicMock()
        mock_requests.get.return_value = mock_resp
        return mock_requests

    def test_uses_qwen_url_by_default(self, tmp_path):
        mock_requests = self._mock_requests()
        with (
            patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path),
            patch.dict("sys.modules", {"requests": mock_requests}),
        ):
            from axiom.setup.llamafile import download_model

            download_model()

            args, kwargs = mock_requests.get.call_args
            url = args[0] if args else kwargs.get("url")
            assert "qwen2.5-7b-instruct" in url.lower()

    def test_legacy_bonsai_passthrough(self, tmp_path):
        mock_requests = self._mock_requests()
        with (
            patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path),
            patch.dict("sys.modules", {"requests": mock_requests}),
        ):
            from axiom.setup.llamafile import download_model

            download_model(model="bonsai")

            args, kwargs = mock_requests.get.call_args
            url = args[0] if args else kwargs.get("url")
            assert "Bonsai-1.7B.gguf" in url

    def test_skips_when_already_cached(self, tmp_path):
        # Pre-create the qwen file
        (tmp_path / "qwen2.5-7b-instruct-q4_k_m.gguf").touch()
        mock_requests = self._mock_requests()
        with (
            patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path),
            patch.dict("sys.modules", {"requests": mock_requests}),
        ):
            from axiom.setup.llamafile import download_model

            result = download_model()
            # No download should happen
            mock_requests.get.assert_not_called()
            assert result == tmp_path / "qwen2.5-7b-instruct-q4_k_m.gguf"


class TestGetStatus:
    def test_returns_correct_dict(self, tmp_path):
        with (
            patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path),
            patch("axiom.setup.llamafile.is_llamafile_installed", return_value=False),
            patch("axiom.setup.llamafile.is_llamafile_running", return_value=False),
        ):
            from axiom.setup.llamafile import get_status

            status = get_status()
            assert status["installed"] is False
            assert status["running"] is False
            assert status["port"] == 8080
            # Default model is qwen now
            assert status["model"] == "qwen2.5-7b-instruct-q4_k_m.gguf"
            assert status["path"] == str(tmp_path)


class TestIsLlamafileRunning:
    def test_returns_false_when_not_running(self):
        from axiom.setup.llamafile import is_llamafile_running

        # Use a port that's almost certainly not in use
        assert is_llamafile_running(port=19999) is False

    def test_returns_true_when_connectable(self):
        with patch("axiom.setup.llamafile.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock()
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)

            from axiom.setup.llamafile import is_llamafile_running

            assert is_llamafile_running() is True


class TestStopLlamafile:
    def test_returns_false_when_no_pid_file(self, tmp_path):
        with patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path):
            from axiom.setup.llamafile import stop_llamafile

            assert stop_llamafile() is False

    def test_kills_process_and_removes_pid(self, tmp_path):
        pid_file = tmp_path / "llamafile.pid"
        pid_file.write_text("12345")

        with (
            patch("axiom.setup.llamafile.get_llamafile_dir", return_value=tmp_path),
            patch("os.kill") as mock_kill,
        ):
            from axiom.setup.llamafile import stop_llamafile

            assert stop_llamafile() is True
            mock_kill.assert_called_once_with(12345, 15)
            assert not pid_file.exists()


# ---------------------------------------------------------------------------
# Infra path detection tests
# ---------------------------------------------------------------------------


class TestDetectInfraPath:
    def test_returns_native_when_no_docker_or_k3d(self):
        with patch("axiom.setup.infra.shutil.which", return_value=None):
            from axiom.setup.infra import detect_infra_path

            assert detect_infra_path() == "native"

    def test_returns_k3d_when_both_available(self):
        def fake_which(name):
            return f"/usr/bin/{name}" if name in ("k3d", "docker") else None

        with patch("axiom.setup.infra.shutil.which", side_effect=fake_which):
            from axiom.setup.infra import detect_infra_path

            assert detect_infra_path() == "k3d"

    def test_returns_docker_compose_when_docker_but_no_k3d(self):
        def fake_which(name):
            return "/usr/bin/docker" if name == "docker" else None

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("axiom.setup.infra.shutil.which", side_effect=fake_which),
            patch("axiom.setup.infra.subprocess.run", return_value=mock_result),
        ):
            from axiom.setup.infra import detect_infra_path

            assert detect_infra_path() == "docker-compose"

    def test_returns_native_when_docker_not_running(self):
        def fake_which(name):
            return "/usr/bin/docker" if name == "docker" else None

        mock_result = MagicMock()
        mock_result.returncode = 1

        with (
            patch("axiom.setup.infra.shutil.which", side_effect=fake_which),
            patch("axiom.setup.infra.subprocess.run", return_value=mock_result),
        ):
            from axiom.setup.infra import detect_infra_path

            assert detect_infra_path() == "native"


# ---------------------------------------------------------------------------
# Native PostgreSQL detection tests
# ---------------------------------------------------------------------------


class TestProvisionPostgresNative:
    def test_detects_running_pg(self):
        with patch("axiom.setup.infra.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock()
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)

            from axiom.setup.infra import provision_postgres_native

            result = provision_postgres_native()
            assert result["running"] is True
            assert result["method"] == "existing"

    def test_suggests_brew_on_macos(self):
        with (
            patch(
                "axiom.setup.infra.socket.create_connection",
                side_effect=ConnectionRefusedError,
            ),
            patch("axiom.setup.infra.shutil.which", return_value="/opt/homebrew/bin/brew"),
        ):
            from axiom.setup.infra import provision_postgres_native

            result = provision_postgres_native()
            assert result["running"] is False
            assert result["method"] == "brew"
            assert any("brew install" in i for i in result["instructions"])

    def test_falls_back_to_manual(self):
        with (
            patch(
                "axiom.setup.infra.socket.create_connection",
                side_effect=ConnectionRefusedError,
            ),
            patch("axiom.setup.infra.shutil.which", return_value=None),
        ):
            from axiom.setup.infra import provision_postgres_native

            result = provision_postgres_native()
            assert result["running"] is False
            assert result["method"] == "manual"


# ---------------------------------------------------------------------------
# Docker Compose provisioning tests
# ---------------------------------------------------------------------------


class TestProvisionPostgresCompose:
    def test_returns_true_when_compose_succeeds(self):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("axiom.setup.infra.subprocess.run", return_value=mock_result), \
             patch("axiom.setup.secrets.get_secret", return_value="test-pw"), \
             patch("axiom.setup.secrets.store_secret", return_value=True):
            from axiom.setup.infra import provision_postgres_compose

            # The real compose file exists in the package — subprocess is mocked
            result = provision_postgres_compose()
            assert result is True

    def test_returns_false_when_compose_fails(self):
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("axiom.setup.infra.subprocess.run", return_value=mock_result), \
             patch("axiom.setup.secrets.get_secret", return_value="test-pw"), \
             patch("axiom.setup.secrets.store_secret", return_value=True):
            from axiom.setup.infra import provision_postgres_compose

            result = provision_postgres_compose()
            assert result is False


def test_small_default_is_shipped_and_not_bonsai():
    """ADR-054: the small-footprint default must be a coherent model, not bonsai."""
    from axiom.setup.llamafile import MODELS, SMALL_MODEL

    assert SMALL_MODEL == "small"
    # ADR-054 + spec-llm-tier-policy: simple-tier / as-shipped default = gemma2:2b
    assert MODELS[SMALL_MODEL]["id"] == "gemma2-2b-it"
    assert MODELS[SMALL_MODEL]["size_gb"] <= 2.0  # lightweight footprint
    # bonsai retained only for migration; must NOT be the shipped small default
    assert SMALL_MODEL != "bonsai"
    assert "DEPRECATED" in MODELS["bonsai"]["description"]
