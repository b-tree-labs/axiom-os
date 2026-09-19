# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.setup.probe."""

from unittest.mock import MagicMock, patch

from axiom.setup.probe import (
    DepStatus,
    ProbeResult,
    _check_python_module,
    _check_tool,
    _probe_dependencies,
    _probe_existing_config,
    _probe_network,
    _probe_project,
    _probe_system,
    run_probe,
)


class TestDepStatus:
    def test_roundtrip(self):
        dep = DepStatus(
            name="git", found=True, version="2.42.0",
            required=True, purpose="Tracks changes",
        )
        d = dep.to_dict()
        restored = DepStatus.from_dict(d)
        assert restored.name == "git"
        assert restored.found is True
        assert restored.version == "2.42.0"
        assert restored.purpose == "Tracks changes"

    def test_from_dict_defaults(self):
        dep = DepStatus.from_dict({"name": "x", "found": False})
        assert dep.version == ""
        assert dep.required is True
        assert dep.purpose == ""


class TestProbeResult:
    def test_roundtrip(self):
        result = ProbeResult(
            os_name="Darwin",
            python_version="3.11.0",
            dependencies=[
                DepStatus(name="git", found=True, version="2.42.0"),
            ],
            env_vars_set={"GITLAB_TOKEN": True},
            config_files_exist={".env": True},
            dns_available=True,
        )
        d = result.to_dict()
        restored = ProbeResult.from_dict(d)
        assert restored.os_name == "Darwin"
        assert restored.python_version == "3.11.0"
        assert len(restored.dependencies) == 1
        assert restored.dependencies[0].name == "git"
        assert restored.env_vars_set["GITLAB_TOKEN"] is True
        assert restored.dns_available is True

    def test_from_dict_empty(self):
        result = ProbeResult.from_dict({})
        assert result.os_name == ""
        assert result.dependencies == []
        assert result.dns_available is False


class TestProbeSystem:
    def test_detects_os(self):
        result = ProbeResult()
        _probe_system(result)
        assert result.os_name != ""
        assert result.python_version != ""
        assert result.cpu_cores > 0


class TestProbeProject:
    def test_git_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        result = ProbeResult()
        _probe_project(result, tmp_path)
        assert result.is_git_repo is True
        assert result.project_root == str(tmp_path)

    def test_not_git_repo(self, tmp_path):
        result = ProbeResult()
        _probe_project(result, tmp_path)
        assert result.is_git_repo is False


class TestCheckTool:
    def test_git_found(self):
        found, version = _check_tool("git", ["git", "--version"])
        assert found is True
        assert "git" in version.lower()

    def test_nonexistent_tool(self):
        found, version = _check_tool("definitely_not_a_real_tool_xyz", ["false"])
        assert found is False


class TestCheckPythonModule:
    def test_json_found(self):
        found, version = _check_python_module("json")
        assert found is True

    def test_nonexistent_module(self):
        found, version = _check_python_module("definitely_not_a_real_module_xyz")
        assert found is False


class TestProbeDependencies:
    def test_finds_some_deps(self):
        result = ProbeResult()
        _probe_dependencies(result)
        assert len(result.dependencies) > 0
        names = [d.name for d in result.dependencies]
        assert "git" in names


class TestProbeExistingConfig:
    def test_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        result = ProbeResult()
        _probe_existing_config(result, tmp_path)
        assert result.env_vars_set["GITLAB_TOKEN"] is True
        assert result.env_vars_set["LINEAR_API_KEY"] is False

    def test_config_files(self, tmp_path):
        (tmp_path / ".env").write_text("x=1")
        result = ProbeResult()
        _probe_existing_config(result, tmp_path)
        assert result.config_files_exist[".env"] is True


class TestProbeNetwork:
    def test_dns_check(self):
        result = ProbeResult()
        _probe_network(result)
        # Just verify it runs without crashing; result depends on network
        assert isinstance(result.dns_available, bool)


class TestRunProbe:
    def test_full_probe(self, tmp_path):
        result = run_probe(tmp_path)
        assert result.os_name != ""
        assert result.python_version != ""
        assert isinstance(result.dependencies, list)
        assert isinstance(result.env_vars_set, dict)


class TestCrossPlatformProbe:
    """Tests that verify probe behavior on simulated Linux and Windows."""

    def test_linux_probe_system(self):
        """Simulate running on Linux."""
        result = ProbeResult()
        with patch("axiom.setup.probe.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            mock_platform.release.return_value = "6.1.0-generic"
            mock_platform.python_version.return_value = "3.11.0"
            # Mock /proc/meminfo for Linux memory detection
            mock_open = MagicMock()
            mock_open.return_value.__enter__ = MagicMock(
                return_value=iter(["MemTotal:       16384000 kB\n", "MemFree:        8000000 kB\n"])
            )
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            with patch("builtins.open", mock_open):
                _probe_system(result)
        assert result.os_name == "Linux"
        assert result.os_version == "6.1.0-generic"
        assert result.memory_gb > 0

    def test_windows_probe_system(self):
        """Simulate running on Windows — memory falls back gracefully."""
        result = ProbeResult()
        with patch("axiom.setup.probe.platform") as mock_platform:
            mock_platform.system.return_value = "Windows"
            mock_platform.release.return_value = "10"
            mock_platform.python_version.return_value = "3.11.0"
            with patch("axiom.setup.probe.os") as mock_os:
                mock_os.cpu_count.return_value = 8
                mock_os.environ = {"COMSPEC": "cmd.exe"}
                _probe_system(result)
        assert result.os_name == "Windows"
        assert result.os_version == "10"
        # Memory detection uses ctypes on Windows — will fail gracefully in test
        # since we're not actually on Windows, but it shouldn't crash
        assert isinstance(result.memory_gb, float)

    def test_probe_no_shell_env_var(self, monkeypatch):
        """Windows doesn't set SHELL — should not crash."""
        monkeypatch.delenv("SHELL", raising=False)
        result = ProbeResult()
        _probe_system(result)
        assert result.shell == ""

    def test_config_files_use_pathlib(self, tmp_path):
        """Config file checks work with forward-slash strings via pathlib."""
        (tmp_path / ".env").write_text("KEY=value")
        result = ProbeResult()
        _probe_existing_config(result, tmp_path)
        assert result.config_files_exist[".env"] is True

    def test_git_not_on_path(self, tmp_path):
        """On a system without git, probe still completes."""
        with patch("axiom.setup.probe.shutil.which", return_value=None):
            found, version = _check_tool("git", ["git", "--version"])
        assert found is False
        assert version == ""
