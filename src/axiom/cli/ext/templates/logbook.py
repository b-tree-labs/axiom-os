# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``logbook`` template for ``ext init --template logbook``.

It writes the compound layout with :func:`scaffold.create`, the one place that knows what AEOS
requires, and then adds a log: ``logbook/<name>.toml``, a ``kind = "logbook"`` block in the
manifest, and a README that says how to check and install it.

The declaration is the example log in ``docs/guides/examples/example_logbook/`` with its id and
name changed. ``tests/logbook_acceptance/test_f3.py`` compares the two, so they cannot drift: change
the example and this text together. The log's id is the extension name, which ``validate_name``
already holds to ``[a-z][a-z0-9_]*``, inside the logbook id rule ``[a-z0-9_]+``.
"""

from __future__ import annotations

from pathlib import Path

#: F2's example declaration with ``__NAME__`` for its id and ``__DISPLAY__`` for its name.
_LOGBOOK = """# The example log: a small, complete logbook declaration that carries no trade.
# Copy the extension that holds it and rename what you need. Every key here is
# one the loader enforces today; `axi log validate` lists nothing
# under "declared, not yet enforced".

[logbook]
id = "__NAME__"
version = "1"
display = "__DISPLAY__"
# The floor every type inherits. `attested` can be signed at the command line.
assurance = { posture = "attested" }

# A period is opened by one signed entry and closed by another. Entries signed
# while it is open carry its number.
[interval.period]
opens = ["OPEN_PERIOD"]
closes = ["CLOSE_PERIOD"]

[[type]]
id = "OPEN_PERIOD"
meanings = ["authored"]
roles = ["member", "lead"]
requires_interval = "none_open"

[[type]]
id = "CHECK"
meanings = ["performed"]
roles = ["member", "lead"]
requires_interval = "period"
fields = [
  # Observed: only the person who signs may enter it; software never fills it.
  { id = "reading", type = "quantity", unit = "kg", observe = true, required = true },
  { id = "confirmed", type = "attest_checkbox", label = "Check completed", required = true },
  { id = "note", type = "text" },
]
confirm = { modalities_any = ["screen", "cli", "voice"], voice_confirm_allowed = true, timeout = "10m", read_back = "on_voice_origin" }

[[type]]
id = "NOTE"
meanings = ["authored"]
roles = ["member", "lead"]
assurance = { posture = "attested" }
fields = [{ id = "text", type = "text", required = true }]

[[type]]
id = "CLOSE_PERIOD"
meanings = ["authored"]
roles = ["lead"]
requires_interval = "period"

# While a period is open, a CHECK is due every 60 minutes unless the site sets
# `__NAME__.checks.interval_minutes`. A miss is an alarm sent to members.
[[obligation]]
id = "periodic_check"
type = "CHECK"
every = { site_key = "__NAME__.checks.interval_minutes", default = 60 }
warn_before = { default = 5 }
while = "interval.period.open"
notify = ["member"]
required = true
"""

_PROVIDES = """
# The log this extension declares. The attest extension finds it here, validates
# the file when the node loads it, and refuses a key it does not know.
[[extension.provides]]
kind = "logbook"
id = "__NAME__"
file = "logbook/__NAME__.toml"
display = "__DISPLAY__"
description = "__DISPLAY__: a period, a check with an observed value, a note, and a cadence"
"""

_README = """# __NAME__

__DESCRIPTION__

This extension declares one log, `__NAME__` ("__DISPLAY__"), in `logbook/__NAME__.toml`. It
starts as a copy of the example log: a period opened by `OPEN_PERIOD` and closed by
`CLOSE_PERIOD`, a `CHECK` with an observed `reading` only the person signing may enter, a `NOTE`,
and a `periodic_check` that is due every 60 minutes while a period is open. Rename the entry
types, fields and roles to fit what your people do. Software may propose an entry; only a person
signs it.

## Check it

```bash
__CLI__ ext lint
__CLI__ ext doctor
__CLI__ log validate logbook/__NAME__.toml
```

`__CLI__ ext doctor` reports `logbook.declared`: whether the log loads, its entry types, and any
key that is declared but not yet enforced.

## Install it

Copy this directory to `<project>/.axi/extensions/__NAME__` and restart the app. Then
`__CLI__ log list` shows it (its handle is the id with `_` read as `-`). A person gets a role at a site with
`__CLI__ log role grant`, for example
`__CLI__ log role grant @you:example-site member --site example-site`.
"""


def _cli() -> str:
    from axiom.cli.ext.templates.scaffold import _cli as scaffold_cli

    return scaffold_cli()


def display_name(name: str) -> str:
    """The name people see: ``field_notes`` is "Field Notes"."""
    return " ".join(part.capitalize() for part in name.split("_") if part)


def logbook_text(name: str) -> str:
    """The declaration the template writes for ``name``.

    The example's comment names the ``axi`` command; a distribution that renames the CLI gets its
    own name there, so with the default brand the text is the example's exactly."""
    text = _LOGBOOK.replace("__NAME__", name).replace("__DISPLAY__", display_name(name))
    return text.replace("`axi ", f"`{_cli()} ")


def create(
    ext_dir, *, name: str, owner: str = "", license: str = "Apache-2.0", description: str = ""
) -> None:
    """Write the compound layout, then the log, its manifest block and its README."""
    from axiom.cli.ext.commands.init import validate_name
    from axiom.cli.ext.templates import scaffold

    problem = validate_name(name)
    if problem:
        raise ValueError(problem)
    display = display_name(name)
    description = description or f"{name}: an extension that declares the {display} log"
    root = Path(ext_dir)
    scaffold.create(root, name=name, owner=owner, license=license, description=description)

    (root / "logbook").mkdir()
    (root / "logbook" / f"{name}.toml").write_text(logbook_text(name), encoding="utf-8")
    manifest = root / "axiom-extension.toml"
    block = _PROVIDES.replace("__NAME__", name).replace("__DISPLAY__", display)
    manifest.write_text(manifest.read_text(encoding="utf-8") + block, encoding="utf-8")
    readme = (
        _README.replace("__DESCRIPTION__", description)
        .replace("__NAME__", name)
        .replace("__DISPLAY__", display)
        .replace("__CLI__", _cli())
    )
    (root / "README.md").write_text(readme, encoding="utf-8")


__all__ = ["create", "display_name", "logbook_text"]
