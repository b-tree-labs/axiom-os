# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The install's own health, as skills: ``doctor.run``, ``binding.status``, ``update.check``, ``update.apply``.

These are the verbs under the onboarding guide's "Or ask chat" lines. They wrap what ``doctor``,
the serving mode and ``update`` already do, so a person (or an agent) can ask in plain English and
get the same answer the command line gives. The one that changes something, ``update.apply``, is a
write: chat confirms before it runs.
"""
