# Install-path validation

Spin up a fresh `python:3.12-slim` container, `pip install axiom-os==<pyproject-pinned-version>`
from the real PyPI index (no local mount, no transitive repo deps), and sanity-check that the
core CLI still works — `axi --version`, `axi --help`, `axi federation init` (cryptography
regression guard), `axi nodes list`, `axi install-shim` subcommand registration — and that
a clean uninstall leaves no importable `axi*` modules behind. This is our defense against the
install-path regressions (cryptography missing, branding squatter, shim fallback) that unit
tests against an editable checkout cannot catch.

## Run

```bash
source ../../../.venv/bin/activate  # repo venv
pytest tests/install_path -m install_path -v
```

Requires Docker + network access to PyPI. Skipped by default (see `addopts` in `pyproject.toml`).
The pinned version is read from `pyproject.toml` at test-time — when you bump the version and
publish, this suite automatically validates the new release.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
