# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CHALKE — AI Training Assistant (ATA) for the classroom.

Dual-role always-on agent: instructor's right hand AND each student's
personal tutor. Ships with the classroom extension (not core) so when
classroom moves to its own repo, CHALKE goes with it.

See SKILLS.md for the agent charter + split of perspectives.
"""

from .chalke import Chalke

__all__ = ["Chalke"]
