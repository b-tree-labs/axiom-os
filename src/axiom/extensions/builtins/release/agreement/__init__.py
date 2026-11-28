# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The owner's agreement in chat, as evidence RIVET can check (#2471, ADR-291).

Shadow mode only: nothing here changes what the lander lands or blocks.

* ``hook``: runs on every submitted prompt and appends it to a local log. The
  harness runs it, not the model, so the log is not something an agent asserts.
* ``store``: the prompt log, and the agreement records that cite a logged prompt.
* ``transcript``: reads the session transcript the harness wrote and says what
  kind of row each one is. Only a row the harness marks as typed by a person is
  the owner.
* ``affirm``: conservative reading of a short reply. No model call.
* ``verify``: AGREED, NOT_AGREED or UNCERTAIN for one pull request.
* ``shadow``: the row the lander records next to its decision, and the replay
  that compares those rows with what the owner then did on GitHub.

Python standard library only, so the hook starts fast and has nothing to break.
"""
