# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Load the module an extension's ``entry`` names (#2008).

Three kinds of extension, two ways to load:

- **Shipped with Axiom** (``builtin = true``): imported as a module.
- **Installed as a package** (``axi ext install`` pip-installs it and links its
  folder for discovery; an editable install of a working copy is the same):
  imported as a module from the package, so its relative imports resolve.
  ``builtin`` keeps its meaning; an installed package does not need it.
- **A folder** with no installed package: loaded from its file under the
  extension folder, as before.

A non-builtin entry is imported as a module only when the module itself is
importable, not merely its top-level name, so a folder extension whose module
shares a top-level name with some unrelated installed package still loads
from its folder.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType


def _installed(module_path: str) -> bool:
    """True when *module_path* resolves on ``sys.path`` (an installed or editable package)."""
    top = module_path.split(".", 1)[0]
    try:
        if importlib.util.find_spec(top) is None:
            return False
        return importlib.util.find_spec(module_path) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def load_entry_module(module_path: str, *, root: str | Path, builtin: bool, alias: str) -> ModuleType:
    """The module an extension's entry names. Raises ImportError when it cannot be loaded."""
    if builtin or _installed(module_path):
        return importlib.import_module(module_path)
    mod_file = Path(str(root)) / module_path.replace(".", "/")
    if mod_file.with_suffix(".py").exists():
        mod_file = mod_file.with_suffix(".py")
    elif (mod_file / "__init__.py").exists():
        mod_file = mod_file / "__init__.py"
    else:
        raise ImportError(f"extension module not found: {module_path}")
    spec = importlib.util.spec_from_file_location(f"neut_ext.{alias}", str(mod_file))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load extension module: {mod_file}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
