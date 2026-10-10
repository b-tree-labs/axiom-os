# data.preflight

Live-verify a registered connector: authenticate with its stored
credentials and confirm the target is reachable, returning
plain-language remediation for anything wrong.

Kind-agnostic — the source kind's provider supplies `preflight`, so one
command verifies Box, GDrive, S3, … identically. Catches the classic
failures (app not authorized, service account not collaborated onto the
folder) at register time instead of at the next sensor tick.

## Params
- `name` (required) — the connector to verify.

## Usage
    axi data preflight dmsr
