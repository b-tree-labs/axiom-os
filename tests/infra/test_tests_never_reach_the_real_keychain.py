# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A test run must not write into the developer's own credential store.

It did, for a long time and quietly. `ForeignCredentialStore(tmp_path)` looks
sandboxed and only its METADATA index is: the values go to
`open_default_value_store`, which prefers the darwin keychain whenever one
exists. So a suite handed a `tmp_path` wrote fixture credentials into the live
`axiom-secrets` namespace, beside that machine's real ones.

Nine of them were found there on one developer's machine — `cred`,
`no-expiry-cred`, `gl-api`, `gl-git`, `gl-orphan`, `gl-agrees`,
`gl-unknown-expiry`, `other-host`, `hand-managed` — every one a fixture name
that appears in a test file.

Two harms, and the second is why this guard exists rather than a cleanup
script. A fake credential in the production namespace is inventory noise that
`axi secrets list` reports as real. And the entries OUTLIVE the run, so the
suite poisons itself: the write is delete-then-create, parallel workers race
on one item name, and the loser fails with "already exists" — green alone,
red in the full suite, on a machine that had run the suite before.
"""

from __future__ import annotations

import os


def test_the_session_pins_a_sandboxed_credential_backend():
    """Set in the root conftest, where `AXIOM_STATE_DIR` is already sandboxed
    for the same reason: a test must not read or write this machine's state."""
    assert os.environ.get("AXIOM_FOREIGN_SECRETS_BACKEND") == "file"


def test_a_foreign_store_opened_without_arguments_is_not_the_keychain(tmp_path):
    """The real assertion. The env var is the mechanism; this is the property
    it buys, and it is what would break if the mechanism were removed or if
    the backend selection grew a new branch that ignored it."""
    from axiom.extensions.builtins.secrets.foreign.store import (
        open_default_value_store,
    )

    store = open_default_value_store(tmp_path)
    assert "keychain" not in type(store).__name__.lower(), (
        f"a test opened {type(store).__name__}, which writes to this machine's "
        "own credential store"
    )
