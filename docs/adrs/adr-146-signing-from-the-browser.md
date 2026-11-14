# ADR-146: Signing from the browser uses a fresh authentication bound to the presented digest

**Status:** Accepted (2026-09-30)
**Amends:** [ADR-123](adr-123-receipts-surface-architecture.md) D3 (browser reads by cookie, writes bearer-only)
**Related:** [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md),
[ADR-143](adr-143-attestations-are-signed-into-a-verifiable-chain.md),
[ADR-144](adr-144-a-person-confirms-exactly-what-is-recorded.md),
[spec-attestation.md](../specs/spec-attestation.md) §Assurance

## Context

ADR-123 D3 lets appkit surfaces read with the webgate session cookie but
requires writes to carry a bearer token. The reason is CSRF: a cookie is sent
by the browser automatically, and a bearer token is not.

Attestation is a browser write by design. An operator signs at a console. A
long-lived bearer token in a console tab would be worse than the cookie it
replaced, because anyone at an unattended console could sign with it.

Separately, step-up exists only in the terminal. `axiom.infra.stepup.step_up`
elevates to `attested` by unlocking the local keypair through the OS keychain,
and refuses non-interactive or `sso` elevation. There is no web path.

## Decision

Signing an attestation from a browser requires a **signing grant**, a
short-lived credential minted for one presented digest:

1. The surface presents the proposal (ADR-144) and obtains its `digest`.
2. The surface requests a grant for `(digest, book, meaning)` from webgate.
3. Webgate requires authentication meeting the book's **assurance** for that
   meaning. Assurance reuses the platform's principal postures
   (`axiom.infra.principal`: open < attested < sso/service). No new ladder is
   invented. A book states three things:
   - **`posture`**: the floor, `sso` (IdP-authenticated) or `attested` (a
     personal key). `open` and `service` principals can never sign.
   - **`fresh_within`**: how recently that authentication must have happened.
     Unset means the current session suffices. With a window, e.g. 5 minutes,
     either webgate performs OIDC re-authentication (`max_age` /
     `prompt=login`), or the site node's speaker verification of the person
     signed in at that console satisfies it (ADR-145).
   - **`personal_key`**: whether the signer's own key must also sign the
     digest. In the browser that is a WebAuthn/passkey assertion whose
     challenge *is* the digest. In the CLI it is the `attested` posture's
     keychain key that `step_up("attested")` already unlocks.
4. Webgate returns a grant bound to the principal, digest, book, meaning,
   console and expiry (≤ 60 s), for one use only.
5. The surface posts the signature request with the grant as its bearer
   credential. The attestation service accepts it only for that digest.

Co-signature by a second person is the same flow run by that person, with
their own authentication, at the same or another console.

Other browser writes remain bearer-only under ADR-123 D3. The signing grant
is the bearer credential for signatures.

### Shared devices

Not every signing surface is a personal session. A device's **class** is
declared when the device is enrolled with the node, and it is bound into
every signing grant with the device id:

| Class | How a person is identified | Fresh factor | Idle policy |
|---|---|---|---|
| Personal session (desktop, console) | Site sign-in | Re-authentication or verified voice | Session lifetime |
| Walk-up kiosk | A badge or token tap claims the device for one person | PIN or verified voice | Public tier when idle; short idle lock; signed out after each signed act |
| Personal tablet | Site sign-in; device lock | PIN or verified voice | Auto-lock |
| Phone | Site sign-in in the app | — | OS lock |

- **Claiming a kiosk** identifies the person; it does not by itself satisfy
  `fresh_within`. The PIN or voice does.
- **Idle lock and sign-out-after-act** are device-class policy, not surface
  code.
- **Presence is a property of the act, not of the person.** A book may
  require that a type be signed on a device at a declared location, for acts
  whose content must be observed there. Devices are enrolled to a location,
  and the site declares how presence is proven. A fixed device proves it by
  enrolment. A portable device proves it per signature with a rotating code
  shown on the location's fixed display, or with a proximity beacon or a
  network segment where a site has one. A rotating code shown at the
  location has a useful property: it proves the signer can see the location,
  which is exactly what an observed value needs. Verified voice establishes *who*
  signs, and presence establishes *where*; the two are independent.
- **Phones may acknowledge.** A reply from a phone is evidence, not a
  signature. A book declares, per meaning, which device classes may sign; the
  default excludes phones.

## Options considered

- **The phone as a presence token** (paired over Bluetooth to a dock or beacon
  at the location). Not required: enrolled fixed devices plus voice already
  prove who and where for the first consumer. Kept as a site-declared option
  for a device that moves.

- **Cookie writes with CSRF tokens.** Lost: a signature would carry no proof
  of recent human presence.
- **Long-lived console bearer tokens.** Lost: the unattended-console problem.
- **Personal keys (WebAuthn) for every signature.** Lost as a default because
  sites will not issue authenticators on day one. Kept as the `personal_key` option for books that
  require it.

## Consequences

- Webgate gains a grant endpoint and an OIDC re-authentication round trip.
  `stepup` gains its web counterpart, so the terminal and the browser reach
  the same postures by their own interactions. The grant is recorded in the attestation's
  `auth_context`.
- Because the grant binds the digest, a compromised surface cannot sign
  content other than what was presented.
- A verified spoken "confirm" (ADR-145) is itself a fresh authentication
  (`amr` = `voice`). Webgate mints the grant from the site node's speaker
  verification result, so voice alone completes signatures that require
  `fresh_within`. Only `personal_key` still needs the key assertion.
