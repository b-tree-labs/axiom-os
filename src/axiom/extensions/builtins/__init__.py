# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Builtin extensions shipped with the platform.

Each subdirectory is a self-contained extension with a axiom-extension.toml
manifest. These are discovered last (lowest precedence) so users can override
any builtin by placing an extension with the same name in their project or
user extension directory.
"""
