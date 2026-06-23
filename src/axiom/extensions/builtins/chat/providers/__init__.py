# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat UI providers — render and input abstraction layer.

Follows the Publisher Factory/Provider pattern. The chat engine works
through RenderProvider and InputProvider ABCs, never importing rich
or prompt_toolkit directly.
"""
