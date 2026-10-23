# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Interactive conversational agent.

Houses **AXI**, the Loop agent in the platform's REPL cycle. AXI
is the user-facing protagonist — humans talk to AXI, and AXI
dispatches to SCAN / CURIO / PRESS / TIDY / TRIAGE as needed. See
``agents/axi/persona.md`` for AXI's role definition.

Consumer layers rebrand the face per Axiomatic Way principle #7
(a consumer layer may surface him under its own name); the underlying agent identity is
canonical.
"""
