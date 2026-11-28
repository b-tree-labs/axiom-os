# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi secrets diagnose` must probe the providers the system actually uses.

It kept a local COPY of `_default_config_for_scheme`, "so the diagnose skill
doesn't import the resolve() entry point". The copy drifted, and a pre-flight
that probes a differently-configured provider is worse than no pre-flight at
all — it answers confidently about something nobody runs.

Two consequences, both observed on a healthy install:

- The copy omitted ``uid``, so every probe logged "Provider 'default-aws'
  has no 'uid' in config — generated uid=…". Eight warnings before the first
  line of output, on an install with nothing wrong with it.
- The copy had branches for openbao, env and kubernetes only. Everything
  else fell through to ``{"name": ...}``, so keychain was probed with no
  ``service``, file with no ``path``, aws with no ``region``. `aws` and
  `file` reported ``available=False`` purely because the probe had not
  given them their configuration.
"""

from __future__ import annotations

import axiom.extensions.builtins.secrets as secrets_pkg
from axiom.extensions.builtins.secrets.skills import diagnose


class TestItAsksTheSameQuestionTheSystemAsks:
    def test_the_config_is_the_canonical_one(self):
        """Not "equivalent to" — the same function. A second copy is what
        drifted the first time."""
        for scheme in ("aws", "azure", "env", "file", "gcp", "keychain", "kubernetes"):
            assert diagnose._default_config_for_scheme(scheme) == (
                secrets_pkg._default_config_for_scheme(scheme)
            ), scheme

    def test_every_scheme_carries_a_uid(self):
        """The absent uid is what produced eight warnings on a clean run."""
        for scheme in ("aws", "azure", "env", "file", "gcp", "keychain", "kubernetes"):
            assert diagnose._default_config_for_scheme(scheme).get("uid"), scheme

    def test_scheme_specific_fields_survive(self):
        """The dropped fields are why `aws` and `file` read as unavailable."""
        assert diagnose._default_config_for_scheme("keychain").get("service")
        assert diagnose._default_config_for_scheme("file").get("path")

    def test_it_still_resolves_without_a_module_level_import(self):
        """The isolation the copy was reaching for: the canonical builder is
        imported inside the call, so module load pulls in no entry point and
        there is no cycle to break."""
        import inspect

        source = inspect.getsource(diagnose._default_config_for_scheme)
        assert "from .. import" in source, "the import must stay inside the function"
        header = inspect.getsource(diagnose).split("def _default_config_for_scheme")[0]
        assert "_default_config_for_scheme as canonical" not in header
