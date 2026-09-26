# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Prague simulator — de-risk v1 classroom end-to-end.

Sim students interact with the composition-integrated pipeline and
CHALKE; every turn passes through the full memory stack; rubric
scores responses; outputs validate Prague readiness.
"""

from .harness import SimResult, run_simulation
from .rubric import RubricScore, score_response
from .sim_student import SimStudent, classroom_of_12

__all__ = [
    "SimStudent", "classroom_of_12",
    "RubricScore", "score_response",
    "SimResult", "run_simulation",
]
