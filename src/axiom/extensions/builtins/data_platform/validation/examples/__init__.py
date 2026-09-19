# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Worked QC checks you copy to start your own. See ``../RECIPE.md``.

Two of them, judging the same model against **different references**, because
that difference is the whole design and it is easier to see in two files than
to describe in one.

``tracks_reference`` asks *is this model right?* — predicted against measured, the
question a validation record answers.

``reproduces_baseline`` asks *did this model change?* — a candidate against the
outputs of a previous revision, the question a refactor needs answered.

They are not competing. They register side by side against one ``model_ref``,
share one runner and one verdict shape, and fail for different reasons with
different people on the hook. If you find yourself building a second comparison
harness, the thing you actually want is a second check.

Both are plain functions. No framework, no base class, no decorator: you can
read every line, call them from a REPL with two dicts, and know exactly what
they do before you trust anything to run them.
"""
