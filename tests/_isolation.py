# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Constants shared by the root conftest and the tests that check what it does."""

#: Port 9 (discard) on loopback: nothing listens there, so a connection is refused at once. The root
#: conftest points the test process here when no database was chosen, so a test that forgot to bind a
#: seam fails loudly instead of writing to a developer's live data.
REFUSING_DB_URL = "postgresql+psycopg2://axiom:axiom@127.0.0.1:9/axiom_db"
