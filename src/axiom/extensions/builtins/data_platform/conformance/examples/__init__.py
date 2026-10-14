# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Worked normalizers you copy to start your own. See ``../RECIPE.md``.

Two of them, because the interesting difference is not what the payload looks
like but **how many signals come out of one record**.

``one_reading`` is the simple case: a record carries a single measurement and
yields a single canonical row.

``wide_frame`` is the case that goes wrong: a record carries many channels at
one timestamp and yields one row per channel. That fan-out is where the
``(row_hash, channel)`` primary key starts to matter, and where a normalizer
that emits the same channel twice loses the second one silently.

Both are plain functions of a dict. No framework, no base class, no I/O: you can
call them from a REPL with one literal and see exactly what comes out.
"""
