# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one place the silver reading tables are named for a write (ADR-265 step 1, #2137).

Silver is renamed in one migration (``silver.signals`` -> ``silver.readings`` and its
companions). Every statement that writes rows (INSERT, UPSERT, UPDATE, DELETE, COPY), every DDL
statement and every catalogue lookup takes the table from here, so the cutover flips one line per
table. The ADR-265 rename guard in ``tests/`` fails a write, DDL statement or catalogue lookup that
names the table literally anywhere else under ``src/``. Read-only SELECTs are not routed yet.

Today's values are unchanged: this module changes no behaviour.
"""

from __future__ import annotations

SILVER_SCHEMA = "silver"

#: The readings table: a hypertable keyed on reading identity. The bare name is for catalogue
#: lookups (``table_name = ...``, ``hypertable_name = ...``); the qualified one is for statements.
SILVER_READINGS_NAME = "signals"
SILVER_READINGS = f"{SILVER_SCHEMA}.{SILVER_READINGS_NAME}"

#: The structured uncertainty terms, one row per (reading, symbol); same ``ts`` as its reading.
SILVER_READING_TERMS_NAME = "signal_uncertainty"
SILVER_READING_TERMS = f"{SILVER_SCHEMA}.{SILVER_READING_TERMS_NAME}"

#: Rows a collapse removes from the readings table are kept here first.
SILVER_READINGS_AUDIT = f"{SILVER_SCHEMA}.signals_identity_audit"

# Index and constraint names (``signals_pkey``, ``signals_reading_identity``) are not table names
# and are deliberately not here.
