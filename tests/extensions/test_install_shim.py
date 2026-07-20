# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ~/.local/bin/axi shim writer.

The shim is a tiny bash script that execs the real venv-installed axi binary,
so non-interactive SSH sessions (federation discovery) can locate axi without
filesystem-walking.
"""

from __future__ import annotations

import stat
from pathlib import Path

from axiom.extensions.builtins.install import shim as shim_mod


def _make_fake_axi(tmp_path: Path, name: str = "venv_a") -> Path:
    venv_bin = tmp_path / name / "bin"
    venv_bin.mkdir(parents=True)
    target = venv_bin / "axi"
    target.write_text("#!/usr/bin/env python3\n# fake axi\n")
    target.chmod(0o755)
    return target


def test_write_shim_creates_executable_script(tmp_path: Path) -> None:
    target = _make_fake_axi(tmp_path)
    home = tmp_path / "home"

    result = shim_mod.write_shim(target_axi=target, home_override=home)

    shim_path = home / ".local" / "bin" / "axi"
    assert shim_path.exists()
    assert shim_path.is_file()
    # Executable for owner
    mode = shim_path.stat().st_mode
    assert mode & stat.S_IXUSR
    assert mode & stat.S_IRUSR
    # Content has correct shebang and exec line
    body = shim_path.read_text()
    assert body.startswith("#!/usr/bin/env bash\n")
    assert f'exec "{target}" "$@"' in body
    assert result.written is True
    assert result.conflict is False
    assert result.path == shim_path


def test_write_shim_is_idempotent(tmp_path: Path) -> None:
    target = _make_fake_axi(tmp_path)
    home = tmp_path / "home"

    first = shim_mod.write_shim(target_axi=target, home_override=home)
    shim_path = first.path
    mtime1 = shim_path.stat().st_mtime_ns

    second = shim_mod.write_shim(target_axi=target, home_override=home)
    mtime2 = shim_path.stat().st_mtime_ns

    assert second.written is False  # no rewrite on identical content
    assert second.conflict is False
    assert mtime1 == mtime2


def test_write_shim_detects_conflict_with_different_target(tmp_path: Path) -> None:
    first_target = _make_fake_axi(tmp_path, name="venv_a")
    second_target = _make_fake_axi(tmp_path, name="venv_b")
    home = tmp_path / "home"

    shim_mod.write_shim(target_axi=first_target, home_override=home)
    result = shim_mod.write_shim(target_axi=second_target, home_override=home)

    assert result.conflict is True
    assert result.previous_target == first_target
    # By default, a conflicting write is NOT overwritten silently.
    shim_path = home / ".local" / "bin" / "axi"
    assert str(first_target) in shim_path.read_text()

    # With force=True, it overwrites.
    forced = shim_mod.write_shim(target_axi=second_target, home_override=home, force=True)
    assert forced.written is True
    assert str(second_target) in shim_path.read_text()


def test_write_shim_creates_local_bin_dir_if_missing(tmp_path: Path) -> None:
    target = _make_fake_axi(tmp_path)
    home = tmp_path / "home"
    assert not (home / ".local" / "bin").exists()

    shim_mod.write_shim(target_axi=target, home_override=home)

    assert (home / ".local" / "bin").is_dir()


def test_path_contains_local_bin_detection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)

    path_with = f"/usr/bin:{local_bin}:/bin"
    path_without = "/usr/bin:/bin"

    assert shim_mod.path_contains_local_bin(local_bin, path_env=path_with) is True
    assert shim_mod.path_contains_local_bin(local_bin, path_env=path_without) is False


def test_cli_invokes_write_shim(tmp_path: Path, capsys, monkeypatch) -> None:
    target = _make_fake_axi(tmp_path)
    home = tmp_path / "home"

    # Point resolve_current_axi at our fake target
    monkeypatch.setattr(shim_mod, "resolve_current_axi", lambda: target)
    monkeypatch.setenv("HOME", str(home))

    from axiom.extensions.builtins.install import shim_cli

    rc = shim_cli.main([])
    assert rc == 0

    shim_path = home / ".local" / "bin" / "axi"
    assert shim_path.exists()
    out = capsys.readouterr().out
    assert "axi" in out


def test_resolve_current_axi_falls_back_to_sys_prefix(
    tmp_path: Path, monkeypatch
) -> None:
    """When sys.argv[0] doesn't resolve and `axi` is not on PATH (typical for
    a venv-installed axi invoked via console_script wrapper from a non-shell
    parent process), resolve should find the venv-installed axi via
    ``sys.prefix/bin/axi``.
    """
    venv_root = tmp_path / "venv"
    venv_bin = venv_root / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python"
    fake_python.write_text("#!/bin/sh\necho fake python\n")
    fake_python.chmod(0o755)
    fake_axi = venv_bin / "axi"
    fake_axi.write_text("#!/usr/bin/env python3\n# fake axi\n")
    fake_axi.chmod(0o755)

    monkeypatch.setattr("sys.argv", ["/some/wrapper/that/is/not/axi"])
    monkeypatch.setattr("sys.executable", str(fake_python))
    monkeypatch.setattr("sys.prefix", str(venv_root))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no axi here

    detected = shim_mod.resolve_current_axi()
    assert detected == fake_axi.resolve(), (
        f"expected fallback to {fake_axi}, got {detected}"
    )


def test_resolve_current_axi_handles_python_symlinked_out_of_venv(
    tmp_path: Path, monkeypatch
) -> None:
    """Linux regression — Ubuntu/Debian venvs symlink bin/python to the
    system python (e.g. /usr/bin/python3). A naive ``Path(sys.executable)
    .resolve().parent / "axi"`` walks out of the venv and lands on a
    non-existent system path; the sibling lookup must use sys.prefix
    so the venv root is preserved.
    """
    # System python lives outside the venv.
    system_bin = tmp_path / "system" / "bin"
    system_bin.mkdir(parents=True)
    real_python = system_bin / "python3"
    real_python.write_text("#!/bin/sh\necho system python\n")
    real_python.chmod(0o755)

    # Venv python is a symlink to system python (mimics Linux behavior).
    venv_root = tmp_path / "venv"
    venv_bin = venv_root / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(real_python)
    fake_axi = venv_bin / "axi"
    fake_axi.write_text("#!/usr/bin/env python3\n# fake axi\n")
    fake_axi.chmod(0o755)

    # The interpreter still reports sys.prefix as the venv root even when
    # bin/python is a symlink — that's exactly what venv guarantees.
    monkeypatch.setattr("sys.argv", [""])  # empty argv0 (e.g. -c invocation)
    monkeypatch.setattr("sys.executable", str(venv_python))
    monkeypatch.setattr("sys.prefix", str(venv_root))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no axi on PATH

    detected = shim_mod.resolve_current_axi()
    # Must resolve to the VENV axi, not the (non-existent) system one.
    assert detected == fake_axi.resolve(), (
        f"expected venv axi {fake_axi}, got {detected} (resolved out of venv?)"
    )
