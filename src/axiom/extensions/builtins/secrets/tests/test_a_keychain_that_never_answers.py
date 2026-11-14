# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A keychain call that never returns is a refusal, not a wait (#1671).

`dev up` in a home with no vault configured sat for minutes with no output:
it was storing the first account's password through the macOS `security`
CLI, which was waiting on a keychain prompt nobody could see, and the call
had no timeout. Every `security` call now has one, and running out of it
raises the keychain's own unavailability error, which names the fix. The
`dev up` refusal (#1790) already turns that error into a clean exit.
"""

from __future__ import annotations

import subprocess

import pytest

from axiom.extensions.builtins.secrets.providers import keychain
from axiom.extensions.builtins.secrets.providers.protocol import SecretRef


def _never_answers(calls):
    def run(args, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    return run


def test_every_security_call_has_a_timeout(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(keychain.subprocess, "run", _never_answers(calls))
    with pytest.raises(keychain.KeychainUnavailable):
        keychain._subprocess_runner(["find-generic-password", "-s", "svc", "-a", "x", "-w"])
    assert calls and calls[0].get("timeout"), "security ran with no timeout"
    assert calls[0]["timeout"] <= 60


def test_a_write_that_never_answers_refuses_and_names_the_fix(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(keychain.subprocess, "run", _never_answers(calls))
    store = keychain._KeychainSecretStore(service="svc", runner=keychain._subprocess_runner)

    with pytest.raises(RuntimeError) as raised:
        store.put(SecretRef(scheme="keychain", path="dev-gate-lane1"), b"never-shown")

    said = str(raised.value)
    assert "AXIOM_FOREIGN_SECRETS_BACKEND" in said
    assert "never-shown" not in said
