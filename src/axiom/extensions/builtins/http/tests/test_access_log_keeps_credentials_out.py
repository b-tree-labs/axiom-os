"""The access log never carries an OAuth code, and a composed app installs that.

Seventeen sign-in codes reached one node's journal in a week, through the
request line uvicorn logs for ``/gate/oidc/callback``.
"""

from __future__ import annotations

import logging

from .. import access_log

CALLBACK = "/gate/oidc/callback?code=0.AXkA-secret&state=abc123&session_state=29b9"


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("10.0.0.1:0", "GET", path, "1.1", 302), None,
    )


def test_the_code_and_state_values_are_redacted():
    record = _access_record(CALLBACK)
    assert access_log.RedactQueryCredentials().filter(record) is True
    line = record.getMessage()
    assert "0.AXkA-secret" not in line and "abc123" not in line and "29b9" not in line
    assert "code=<redacted>" in line and "/gate/oidc/callback" in line


def test_an_ordinary_query_is_untouched():
    record = _access_record("/api/v1/sites/x/series?bucket_s=60&channels=a,b")
    access_log.RedactQueryCredentials().filter(record)
    assert "bucket_s=60&channels=a,b" in record.getMessage()


def test_a_parameter_that_merely_ends_in_code_is_untouched():
    record = _access_record("/x?zipcode=78712")
    access_log.RedactQueryCredentials().filter(record)
    assert "zipcode=78712" in record.getMessage()


def test_composing_the_app_installs_it_once():
    from ..compose import compose_app

    logger = logging.getLogger("uvicorn.access")
    compose_app(include_builtins=False, auto_authz=False, allow_insecure=True)
    compose_app(include_builtins=False, auto_authz=False, allow_insecure=True)
    installed = [f for f in logger.filters if isinstance(f, access_log.RedactQueryCredentials)]
    assert len(installed) == 1
