# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A shell install step is an argument list. A pipeline is not run."""

from __future__ import annotations

from axiom.extensions.builtins.install.installer import InstallStep, _run_shell


def test_a_plain_command_is_executed_as_a_list(monkeypatch):
    seen = {}

    def fake(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["shell"] = kwargs.get("shell", False)

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(
        "axiom.extensions.builtins.install.installer.subprocess.run", fake
    )
    step = InstallStep(id="s", type="shell", command="echo hello")
    assert _run_shell(step) is True
    assert seen == {"argv": ["echo", "hello"], "shell": False}


def test_a_pipeline_is_refused(monkeypatch):
    def fake(*_args, **_kwargs):
        raise AssertionError("must not run")

    monkeypatch.setattr(
        "axiom.extensions.builtins.install.installer.subprocess.run", fake
    )
    step = InstallStep(id="s", type="shell", command="echo hello | sh")
    assert _run_shell(step) is False
