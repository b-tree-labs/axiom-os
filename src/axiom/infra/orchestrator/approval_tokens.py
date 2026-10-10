# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Single-use secrets that let a person answer an approval from email or SMS.

A held action needs a human, and the human is not at the terminal. Sending them
a link or a code is easy; making the answer mean something is the hard part,
because the answer has to prove it came from a person rather than from the
agent that proposed the work.

What a token actually controls, and what it does not
-----------------------------------------------------

**A nonce is only a control against a party that cannot observe the channel it
was delivered on.** That sentence decides where this is useful:

- **Email and SMS — yes.** The agent has no mailbox and no handset. It cannot
  read the token, so it cannot spend it. This is the case this module exists
  for.
- **Slack and Teams — redundant.** The platform already attests who clicked,
  and ``ApprovalOutcome.actor`` is a stronger claim than possession of a
  secret. Harmless to add, but it is not what is doing the work.
- **A terminal chat, or an agent session — no, and it must not be offered.**
  The agent reads the transcript. A token rendered there is a secret handed to
  the party it is meant to exclude. Approving from a surface the agent can
  observe has to rest on something else: being the person at the keyboard.

:func:`mint` therefore takes the delivery channel and refuses the ones where a
token would be theatre.

Stored hashed
-------------

Only the digest is written down. The plaintext exists once, in the return value
of :func:`mint`, and is never recoverable afterwards — so the queue file is not
a list of working keys if it leaks, and neither is a backup of it.

The other reason is nearer to home: ``approval.pending`` is an agent-reachable
capability, deliberately, so an agent can say "I am waiting on you" rather than
stalling. A token on the ``Action`` would be a token an agent could read out of
its own queue. They live in a separate store that no capability projects.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.infra.state import LockedJsonFile

__all__ = [
    "AMBIGUOUS",
    "CODE_ALPHABET",
    "DEFAULT_TTL_HOURS",
    "MAX_ATTEMPTS",
    "REMOTE_CHANNELS",
    "ApprovalToken",
    "TokenStore",
    "UnsupportedChannel",
    "default_token_path",
]

#: Channels where the agent cannot see what was delivered. Only these get a
#: token; see the module docstring on why the others would be theatre.
REMOTE_CHANNELS = frozenset({"email", "sms"})

#: Characters a person retypes off a phone screen without thinking about them.
#:
#: Excludes each side of the four pairs that get misread in a glance: 0/O,
#: 1/I/L, 5/S and 8/B. The first version of this alphabet still contained an
#: 8, so it contradicted its own docstring and produced a code with an
#: ambiguous character roughly one time in four — which is also why the test
#: caught it only on a later run. AMBIGUOUS below is checked against this at
#: import, so the two cannot drift again.
CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY234679"

#: The characters CODE_ALPHABET exists to avoid.
AMBIGUOUS = "0O1IL5S8B"

assert not (set(CODE_ALPHABET) & set(AMBIGUOUS)), (
    "CODE_ALPHABET contains a character it is meant to exclude: "
    f"{''.join(sorted(set(CODE_ALPHABET) & set(AMBIGUOUS)))}"
)

#: A code is short, so it is guessable if you are allowed to keep guessing.
#: Wrong answers are counted against the action, not the token, because an
#: attacker guessing does not know which token they are failing against.
MAX_ATTEMPTS = 5

#: Shorter than the 72h an action itself lives. A token is a key in transit;
#: the action outliving it means a late answer is refused rather than the whole
#: request being lost, and the person can be sent a fresh one.
DEFAULT_TTL_HOURS = 24.0


class UnsupportedChannel(ValueError):
    """A token was requested for a channel the agent can observe.

    Raised rather than quietly returning ``None``: a caller that asked for a
    token and got nothing back would most likely send the approval anyway, with
    no control at all, believing there was one.
    """


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def default_token_path() -> Path:
    """Beside the approval queue, in the same branding-aware state directory."""
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "orchestrator" / "approval_tokens.json"


@dataclass
class ApprovalToken:
    """What is written down. Note the absence of the secret itself."""

    digest: str
    action_id: str
    channel: str
    expires_at: str
    used_at: str | None = None
    attempts: int = 0
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "digest": self.digest,
            "action_id": self.action_id,
            "channel": self.channel,
            "expires_at": self.expires_at,
            "used_at": self.used_at,
            "attempts": self.attempts,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ApprovalToken:
        return cls(
            digest=d["digest"],
            action_id=d["action_id"],
            channel=d.get("channel", ""),
            expires_at=d.get("expires_at", ""),
            used_at=d.get("used_at"),
            attempts=int(d.get("attempts", 0)),
            meta=d.get("meta", {}),
        )


class TokenStore:
    """Mint and redeem, durably, across processes.

    File-backed for the same reason the approval queue is: the process that
    sends the email is not the process that handles the reply, and neither is
    guaranteed to be alive when the person finally answers.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_hours: float = DEFAULT_TTL_HOURS,
        now: datetime | None = None,
    ) -> None:
        self._path = Path(path) if path else default_token_path()
        self._ttl = timedelta(hours=ttl_hours)
        self._now_override = now

    def _now(self) -> datetime:
        return self._now_override or datetime.now(UTC)

    def _read(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        with LockedJsonFile(self._path, exclusive=False) as handle:
            data = handle.read()
            raw = self._path.read_text(encoding="utf-8")
        if data == {} and raw.strip() not in ("", "{}"):
            raise RuntimeError(
                f"the approval token store at {self._path} did not parse. "
                "Refusing to report that nobody has a valid token."
            )
        return data if isinstance(data, dict) else {}

    def _write(self, mutate) -> None:
        with LockedJsonFile(self._path, exclusive=True) as handle:
            data = handle.read()
            if not isinstance(data, dict):
                data = {}
            mutate(data)
            handle.write(data)

    def mint(self, action_id: str, *, channel: str, meta: dict | None = None) -> str:
        """Create a token for ``action_id`` and return the plaintext, once.

        Raises :class:`UnsupportedChannel` for anything the agent could read.
        """
        if channel not in REMOTE_CHANNELS:
            raise UnsupportedChannel(
                f"no approval token for channel {channel!r}. A token only controls a "
                f"party that cannot observe the channel it was delivered on, and "
                f"{channel!r} is not one of {sorted(REMOTE_CHANNELS)}. Approving from a "
                "surface the agent can read has to rest on being the person at the "
                "keyboard, not on a secret the agent can also see."
            )

        secret = (
            secrets.token_urlsafe(32)
            if channel == "email"
            else "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        )
        token = ApprovalToken(
            digest=_digest(secret),
            action_id=action_id,
            channel=channel,
            expires_at=(self._now() + self._ttl).isoformat(),
            meta=meta or {},
        )
        self._write(lambda data: data.__setitem__(token.digest, token.to_dict()))
        return secret

    def redeem(self, secret: str) -> str | None:
        """Spend ``secret`` and return the action it answers, or ``None``.

        ``None`` for every failure — unknown, expired, already used, too many
        attempts. The caller gets one bit, because telling a guesser *why* a
        code failed tells them whether they are close.
        """
        digest = _digest(secret)
        result: dict[str, str | None] = {"action_id": None}

        def _spend(data: dict) -> None:
            record = None
            for stored_digest, value in data.items():
                # Constant-time even though a dict lookup would be simpler:
                # the digest is derived from a secret and the comparison is on
                # the hot path of a guessing attempt.
                if hmac.compare_digest(stored_digest, digest):
                    record = value
                    break
            if record is None:
                return

            token = ApprovalToken.from_dict(record)
            if token.used_at is not None:
                return
            if token.attempts >= MAX_ATTEMPTS:
                return
            try:
                expires = datetime.fromisoformat(token.expires_at)
            except ValueError:
                return
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if self._now() > expires:
                return

            token.used_at = self._now().isoformat()
            data[token.digest] = token.to_dict()
            result["action_id"] = token.action_id

        self._write(_spend)
        return result["action_id"]

    def record_failure(self, action_id: str) -> None:
        """Count a wrong answer against every live token for ``action_id``.

        Against the action rather than the token, because a guesser supplying
        a code that matches nothing has not failed against any particular
        token — and counting only successful lookups would leave guessing
        uncounted, which is the whole attack.
        """

        def _bump(data: dict) -> None:
            for digest, value in list(data.items()):
                if value.get("action_id") == action_id and value.get("used_at") is None:
                    value["attempts"] = int(value.get("attempts", 0)) + 1
                    data[digest] = value

        self._write(_bump)

    def live_for(self, action_id: str) -> list[ApprovalToken]:
        """Unused, unexpired tokens for an action. For tests and for re-send."""
        now = self._now()
        out = []
        for value in self._read().values():
            token = ApprovalToken.from_dict(value)
            if token.action_id != action_id or token.used_at is not None:
                continue
            try:
                expires = datetime.fromisoformat(token.expires_at)
            except ValueError:
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if now <= expires and token.attempts < MAX_ATTEMPTS:
                out.append(token)
        return out

    def purge_spent(self) -> int:
        """Drop used and expired tokens. Returns how many went."""
        removed = 0
        now = self._now()

        def _purge(data: dict) -> None:
            nonlocal removed
            keep = {}
            for digest, value in data.items():
                token = ApprovalToken.from_dict(value)
                try:
                    expires = datetime.fromisoformat(token.expires_at)
                except ValueError:
                    expires = now
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if token.used_at is None and now <= expires:
                    keep[digest] = value
                else:
                    removed += 1
            data.clear()
            data.update(keep)

        self._write(_purge)
        return removed


def redeem_and_decide(
    secret: str,
    gate,
    *,
    approving: bool,
    decided_by: str,
    store: TokenStore | None = None,
    reason: str = "",
):
    """Spend a token and settle the action it answers. Returns the action or None.

    The one entry point an email or SMS handler needs, so neither has to know
    the order of operations. The token is spent first: a redeem that succeeds
    and a decision that then fails must not leave a live token behind, because
    the second attempt would be a second chance at a secret that was already
    used once in the open.
    """
    tokens = store or TokenStore()
    action_id = tokens.redeem(secret)
    if action_id is None:
        return None
    if approving:
        return gate.approve(action_id, decided_by=decided_by)
    return gate.reject(action_id, reason or "rejected by token", decided_by=decided_by)
