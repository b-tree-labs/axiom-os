# SKILL: gate.role

**Owner:** `axi gate role` · invocable through SkillRegistry (ADR-056)
**Kind:** skill (function-backed)
**Status:** active

## What this skill does

Changes roles on one account (`axi gate role you@site --set reviewer`) or on
every account holding a role (`axi gate role --where-role viewer --add reviewer
--remove viewer`). A running gate applies the change on the person's next request: the session
they hold is read against the account each time, so nobody signs in again.

## Safety

Refuses a role the node does not define (built-in or the site's roles file),
refuses an unknown account rather than creating one, and never touches
passwords, names or identity bindings. Re-running is a no-op.
