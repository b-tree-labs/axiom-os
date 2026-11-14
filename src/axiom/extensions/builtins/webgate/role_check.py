# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Does each persona role reach what it should, and nothing more? The release check (ADR-222).

A role can quietly lack a permission: the first colleague who signed in on 2026-10-26 made a
self-service key, and every data read answered 403, because a data question arrives as POST
and the role only read. Nobody had asked the role to do what the person came to do. This
module asks, for each of the three persona roles, with a real self-service key:

========== ========= ========= ===== ========== ==========
role        data read documents chat  kit submit governance
========== ========= ========= ===== ========== ==========
reader      yes       yes       yes   no         no
contributor yes       yes       yes   yes        no
site-admin  yes       yes       yes   yes        yes
========== ========= ========= ===== ========== ==========

``site-admin`` was called ``operator`` until ADR-261; ``--role`` still accepts the old name.

Two ways to run it, both from the command line and both with ``--role`` (repeatable; all three
when omitted):

**In process** (the default; what the release workflow runs). A real session is signed for an
account holding the role, the gate's real ``POST /gate/keys`` mints the key, and the key is
presented to a composed app through the real authz middleware. The mounts keep their real
``reads`` and ``governs`` declarations; their handlers are replaced by ones that only answer
"reached", so nothing touches a database or a model::

    python -m axiom.extensions.builtins.webgate.role_check --role reader

**Against a live node** (for a CI job that stands up a node and walks the onboarding guide).
Either sign in as an account that holds the role, which mints a real self-service key through
the node's own gate, or present a key already minted::

    python -m axiom.extensions.builtins.webgate.role_check --role contributor \\
        --node http://127.0.0.1:8770 --email someone@example.org --password-env ROLE_PASSWORD

    python -m axiom.extensions.builtins.webgate.role_check --role reader \\
        --node http://127.0.0.1:8770 --key-env ROLE_KEY

Every live probe is a request the node refuses before it changes anything when the role may
reach it (an unknown verb on the submit and review paths, an empty chat), so the check writes
nothing. A probe the node does not serve (404) is reported as not served, not as a pass.

The exit status is 0 when every role reached exactly what it should, 1 otherwise. A key or a
password is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The site the in-process check runs as. Invented, as every example here is.
EXAMPLE_SITE = "example-loop"


@dataclass(frozen=True)
class Probe:
    """One representative act, as the request a client really sends for it."""

    act: str
    what: str
    method: str
    path: str
    body: dict[str, Any] | None = None


#: What each act is, at the door. Each live body is refused before any change when reached.
PROBES: tuple[Probe, ...] = (
    Probe("data read", "a data question", "POST", "/data/query", {"verb": "data.tables", "params": {}}),
    Probe("documents", "a document search", "POST", "/api/v1/rag/search", {}),
    Probe("documents", "the library listing", "GET", "/api/v1/library/items"),
    Probe("chat", "a chat completion", "POST", "/v1/chat/completions", {}),
    Probe("kit submit", "a kit submission", "POST", "/data/submit",
          {"verb": "data.role_check_probe", "params": {}}),
    Probe("governance", "the review queue", "POST", "/data/review",
          {"verb": "data.role_check_probe", "params": {}}),
)

ACTS: tuple[str, ...] = ("data read", "documents", "chat", "kit submit", "governance")

#: What each persona role reaches. Anything not listed it must be refused.
REACHES: dict[str, frozenset[str]] = {
    "reader": frozenset({"data read", "documents", "chat"}),
    "contributor": frozenset({"data read", "documents", "chat", "kit submit"}),
    "site-admin": frozenset(ACTS),
}


def _persona(role: str) -> str:
    """``role`` in its current id (``operator`` is ``site-admin``, ADR-261)."""
    from axiom.governance.roles import canonical_role

    return canonical_role(role)


@dataclass
class ProbeResult:
    probe: Probe
    status: int
    #: ``reached``, ``refused`` or ``not served``.
    outcome: str


@dataclass
class RoleReport:
    role: str
    scopes: tuple[str, ...] = ()
    results: list[ProbeResult] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "ok": self.ok,
            "scopes": list(self.scopes),
            "results": [
                {"act": r.probe.act, "what": r.probe.what, "method": r.probe.method,
                 "path": r.probe.path, "status": r.status, "outcome": r.outcome}
                for r in self.results
            ],
            "problems": list(self.problems),
        }


def _outcome(status: int) -> str:
    if status in (401, 403):
        return "refused"
    if status == 404:
        return "not served"
    return "reached"


def judge(role: str, results: Iterable[ProbeResult]) -> list[str]:
    """What is wrong with ``results`` for ``role``, in words. Empty: the role is right."""
    expected = REACHES.get(_persona(role))
    if expected is None:
        return [f"{role!r} is not a persona role; expected one of {', '.join(REACHES)}"]
    problems: list[str] = []
    for r in results:
        should = r.probe.act in expected
        if r.outcome == "not served":
            continue  # said in the table; a node that lacks a door cannot prove or disprove a role
        if should and r.outcome != "reached":
            problems.append(f"{role} should reach {r.probe.what} ({r.probe.method} {r.probe.path}) "
                            f"and was refused (HTTP {r.status})")
        if not should and r.outcome == "reached":
            problems.append(f"{role} must not reach {r.probe.what} ({r.probe.method} {r.probe.path}) "
                            f"and did (HTTP {r.status})")
    return problems


# ---------------------------------------------------------------------------- in process


def _stub_router(prefix: str):
    """A router that answers every path under ``prefix`` with 200: the request got past the gate."""
    from fastapi import APIRouter

    router = APIRouter()

    async def reached() -> dict[str, bool]:
        return {"reached": True}

    for path in (prefix, prefix + "/{rest:path}"):
        router.add_api_route(path, reached, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    return router


def _real_mounts():
    """The mounts the probes address, as the node declares them, with stub handlers.

    Only the router is replaced: extension, prefix, ``reads``, ``governs`` and nesting are the
    shipped declarations, so a change to any of them is what this check sees.
    """
    from dataclasses import replace

    from axiom.extensions.builtins.data_platform.services.read_mount import mount_spec as data_mount
    from axiom.extensions.builtins.http.mounts import chat_mount_spec, rag_search_mount_spec
    from axiom.extensions.builtins.library.mount import mount_spec as library_mount

    specs = (data_mount(), rag_search_mount_spec(), library_mount(), chat_mount_spec())
    return [replace(s, router=_stub_router(s.prefix), requires_authz=True, profiles=(),
                    bind=None, trust_zone=None)
            for s in specs]


class _Env:
    """Set environment variables for the length of a ``with`` block, then put them back."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self) -> _Env:
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc: object) -> None:
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _ephemeral_signing_key() -> str:
    """A fresh P-256 key in PEM, so the session is signed without reaching for any vault."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")


def check_in_process(roles: Sequence[str]) -> list[RoleReport]:
    """Mint a real self-service key per role and present it to the composed app's real gate."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
    from axiom.extensions.builtins.http.compose import compose_app
    from axiom.extensions.builtins.http.middleware import MiddlewareConfig
    from axiom.extensions.builtins.http.registry import RouterRegistry
    from axiom.governance import Decision
    from axiom.governance.verdict import Verdict
    from axiom.webauth import keys as webauth_keys
    from axiom.webauth.api_keys import JsonFileApiKeyStore
    from axiom.webauth.session import SESSION_COOKIE, issue_session_token
    from axiom.webauth.users import User

    from .api.routers import build_webgate_router

    reports: list[RoleReport] = []
    with tempfile.TemporaryDirectory(prefix="axiom-role-check-") as tmp:
        keys_file = Path(tmp) / "api-keys.json"
        env = {
            "AXIOM_GATE_API_KEYS_FILE": str(keys_file),
            "AXIOM_SITE": EXAMPLE_SITE,
            "AXIOM_GATE_ISSUER": "http://testserver",
            "AXIOM_GATE_INSECURE_COOKIE": "1",
            "WEBAUTH_SIGNING_KEY": _ephemeral_signing_key(),
            "ENVIRONMENT": "development",
        }
        with _Env(env):
            webauth_keys.reset_key_store_for_tests()
            try:
                gate = FastAPI()
                gate.include_router(build_webgate_router(oidc=None, secure_cookies=False))
                gate_client = TestClient(gate)

                registry = RouterRegistry()
                for spec in _real_mounts():
                    registry.register(spec)
                # Scope enforcement runs before ``decide`` and does not depend on the engine, so
                # a permissive engine here proves exactly what the key's scopes allow (the hook's
                # contract: "a permissive or absent engine can never widen an issued key").
                hook = build_authz_hook(
                    resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(keys_file)),
                    decide_fn=lambda env: Verdict.from_decision(Decision.PERMIT, "role check", "role-check"),
                )
                app = compose_app(registry=registry, manifests=[], include_builtins=False,
                                  auto_authz=False, middleware=MiddlewareConfig(authz=hook))
                client = TestClient(app)

                for role in roles:
                    report = RoleReport(role=role)
                    user = User(user_id=f"role-check-{role}", email=f"{role}@example.org",
                                password_hash=None, name=f"Role check ({role})", roles=(role,),
                                site=EXAMPLE_SITE)
                    session = issue_session_token(user, issuer="http://testserver")
                    gate_client.cookies.set(SESSION_COOKIE, session)
                    minted = gate_client.post("/gate/keys", json={})
                    gate_client.cookies.clear()
                    if minted.status_code != 200:
                        report.problems.append(
                            f"{role}: the gate would not mint a self-service key "
                            f"(HTTP {minted.status_code}: {minted.json().get('detail', '')})")
                        reports.append(report)
                        continue
                    body = minted.json()
                    report.scopes = tuple(body.get("scopes") or ())
                    headers = {"authorization": f"Bearer {body['token']}"}
                    for probe in PROBES:
                        reply = client.request(probe.method, probe.path, headers=headers,
                                               json=probe.body if probe.method != "GET" else None)
                        report.results.append(ProbeResult(probe, reply.status_code, _outcome(reply.status_code)))
                    report.problems += judge(role, report.results)
                    reports.append(report)
            finally:
                webauth_keys.reset_key_store_for_tests()
    return reports


# ---------------------------------------------------------------------------- live node


def _http(method: str, url: str, *, headers: dict[str, str] | None = None,
          data: bytes | None = None, timeout: float = 30.0) -> tuple[int, dict[str, str], bytes]:
    """One request; never follows a redirect (a sign-in answers 303 with the cookie)."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a: Any, **k: Any) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b""


def mint_by_signing_in(node: str, email: str, password: str) -> tuple[str, dict[str, Any]]:
    """Sign in at the node's gate and mint a self-service key there. ``(token, the key's record)``."""
    import http.cookies
    from urllib.parse import urlencode

    from axiom.webauth.session import SESSION_COOKIE

    status, headers, _ = _http(
        "POST", f"{node}/gate/login",
        headers={"content-type": "application/x-www-form-urlencoded"},
        data=urlencode({"email": email, "password": password, "next": "/"}).encode("utf-8"))
    raw = next((v for k, v in headers.items() if k.lower() == "set-cookie"), "")
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(raw)
    except http.cookies.CookieError:
        pass
    session = jar.get(SESSION_COOKIE)
    if status != 303 or session is None:
        raise RuntimeError(f"signing in at {node}/gate/login did not give a session (HTTP {status}); "
                           "check the account and its password")
    status, _, body = _http(
        "POST", f"{node}/gate/keys",
        headers={"content-type": "application/json", "cookie": f"{SESSION_COOKIE}={session.value}"},
        data=b"{}")
    record = json.loads(body or b"{}")
    if status != 200 or "token" not in record:
        raise RuntimeError(f"{node}/gate/keys would not mint a key (HTTP {status}): {record.get('detail', '')}")
    return str(record.pop("token")), record


def check_live(role: str, node: str, token: str, *, scopes: Sequence[str] = ()) -> RoleReport:
    """Present ``token`` to a running node for each probe and judge the answers for ``role``."""
    node = node.rstrip("/")
    report = RoleReport(role=role, scopes=tuple(scopes))
    for probe in PROBES:
        headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
        data = None
        if probe.method != "GET":
            headers["content-type"] = "application/json"
            data = json.dumps(probe.body or {}).encode("utf-8")
        try:
            status, _, _ = _http(probe.method, f"{node}{probe.path}", headers=headers, data=data)
        except OSError as exc:
            report.problems.append(f"{node} could not be reached for {probe.what}: {exc}")
            return report
        report.results.append(ProbeResult(probe, status, _outcome(status)))
    report.problems += judge(role, report.results)
    return report


# ---------------------------------------------------------------------------- output


def render(reports: Sequence[RoleReport]) -> str:
    from axiom.infra.cli_format import Column, table

    lines = []
    columns = [Column("act"), Column("outcome"), Column("should"), Column("status"),
               Column("request", wrap=True)]
    for rep in reports:
        lines.append(f"{rep.role}: {'ok' if rep.ok else 'WRONG'}"
                     + (f"  (scopes: {', '.join(rep.scopes)})" if rep.scopes else ""))
        rows = [(r.probe.act, r.outcome,
                 "reach" if r.probe.act in REACHES.get(_persona(rep.role), ()) else "refuse",
                 f"HTTP {r.status}", f"{r.probe.method} {r.probe.path}")
                for r in rep.results]
        if rows:
            lines += table(rows, columns)
        lines += [f"  problem: {p}" for p in rep.problems]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m axiom.extensions.builtins.webgate.role_check",
        description="Check that each persona role reaches what it should with a real self-service key.")
    parser.add_argument("--role", action="append", type=_persona, choices=sorted(REACHES),
                        help="a persona role to check (repeatable; all three when omitted); "
                             "'operator' is accepted as site-admin")
    parser.add_argument("--node", help="a running node's address; without it the check runs in process")
    parser.add_argument("--email", help="with --node: sign in as this account to mint the key")
    parser.add_argument("--password-env", metavar="VAR",
                        help="with --email: the environment variable holding that account's password")
    parser.add_argument("--key-env", metavar="VAR",
                        help="with --node: the environment variable holding a key already minted")
    parser.add_argument("--json", action="store_true", help="print the reports as JSON")
    args = parser.parse_args(argv)
    roles = args.role or list(REACHES)

    if args.node:
        if len(roles) != 1:
            parser.error("--node checks one --role at a time: the key or the account holds one role")
        if args.key_env:
            token = os.environ.get(args.key_env, "").strip()
            if not token:
                parser.error(f"--key-env {args.key_env}: that variable is empty")
            reports = [check_live(roles[0], args.node, token)]
        elif args.email and args.password_env:
            password = os.environ.get(args.password_env, "")
            if not password:
                parser.error(f"--password-env {args.password_env}: that variable is empty")
            try:
                token, record = mint_by_signing_in(args.node.rstrip("/"), args.email, password)
            except RuntimeError as exc:
                print(f"role check: {exc}", file=sys.stderr)
                return 1
            reports = [check_live(roles[0], args.node, token, scopes=record.get("scopes") or ())]
        else:
            parser.error("--node needs --key-env, or --email with --password-env")
    else:
        reports = check_in_process(roles)

    print(json.dumps([r.as_dict() for r in reports], indent=2) if args.json else render(reports))
    return 0 if all(r.ok for r in reports) else 1


__all__ = ["ACTS", "PROBES", "REACHES", "Probe", "ProbeResult", "RoleReport",
           "check_in_process", "check_live", "judge", "main", "mint_by_signing_in", "render"]


if __name__ == "__main__":
    raise SystemExit(main())
