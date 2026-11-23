---
name: synthetic.run
description: Run one scripted user-journey check against a live web surface and return pass, fail or unproven per step
allowed-tools: []
---

# synthetic.run

Runs a journey (a TOML file or an inline dict) over HTTP and reports what the
server said at each step: status, redirects, cache headers, content types.

## Params

- `path` (string) — a journey TOML file. Or `journey` (dict) with the same shape.
- `base_url` (string, optional) — override the journey's target.
- `env` (dict, optional) — environment used to resolve the session cookie
  (default: the process environment).

## Returns

`value.status` is `pass`, `fail` or `unproven`. `ok` only means the skill ran:
a gate must require `value.status == "pass"`. `unproven` means a step could not
be run (for example the session cookie variable is not set); it is never a pass.

`value.steps[]` carries each step's `status`, a plain-language `detail`, and
`evidence` (status code, latency, selected headers). Cookie values are never
recorded.
