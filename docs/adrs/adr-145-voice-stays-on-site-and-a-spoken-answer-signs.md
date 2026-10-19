# ADR-145: Voice stays on site, starts on the agent's name, and a spoken answer signs

**Status:** Accepted (2026-09-30)
**Related:** [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md),
[ADR-144](adr-144-a-person-confirms-exactly-what-is-recorded.md),
[spec-attestation.md](../specs/spec-attestation.md) §Voice

## Context

Books used at a console (equipment operations, maintenance at the equipment) need
hands-free entry: a spoken phrase starts recording, the speech becomes an entry,
and the entry is read back for confirmation.

What exists:

- appkit's `useDictation` does push-to-talk dictation for the chat composer.
  Its instant layer is the browser Web Speech API. In Chrome that API sends
  audio to the browser vendor's servers.
- The signals extension transcribes voice memos with Whisper on the node.
- There is no wake-phrase detection and no speech output.

Three constraints decide the design:

1. **Audio from an operations room must not leave the site.** Sites have said the
   originals of their records stay in the building. Speech can carry
   controlled technical content.
2. **Speech is error-prone in exactly the ways that matter:** numbers, units,
   identifiers, and a radio or conversation that happens to contain the wake
   phrase.
3. **Dependencies must be licensed no more restrictively than Apache-2.0.**
   Several popular wake-word engines and pretrained wake models are
   proprietary or non-commercial.

## Decision

- **The wake word is the agent's name, and speech follows the CLI's
  grammar.** People talk to the node's agent by name: `Axi` on a base node,
  and a consumer names its own agent. After the name comes the same
  noun-then-verb path as the command line (`<agent> <noun> <verb>`), and the
  agent orients one level at a time:
  - the name alone gets "How can I help?";
  - a noun gets its most likely verb as a question (a logbook noun: "New
    entry?");
  - a verb gets the book's entry kinds;
  - an experienced speaker says the whole path in one breath and skips the
    questions.
  Spoken nouns map to CLI nouns through a per-consumer alias table, e.g. a
  spoken "ops log" to the CLI noun `ops`. One vocabulary serves typing,
  speaking and chat, and a noun the CLI does not have cannot be spoken.
- **Configurable, detected on the device.** Each site may set an end phrase
  and a push-to-talk key, and may disable the wake word per console.
  Detection runs locally on the console, in the browser via WebAssembly or
  ONNX, or in a small console agent. Audio before the wake phrase is never
  buffered beyond the detector's window and never transmitted. Push-to-talk
  (key, button, pedal) is always available as an alternative. A site may
  disable the wake phrase.
- **Recording is always visible and audible:** a start tone, a persistent
  on-screen indicator, and an end tone. Recording ends on the end phrase, on
  silence, or on push-to-talk release.
- **Transcription on the site node.** Audio goes only to the node's speech
  service, a local Whisper-family model. Browser speech services that send
  audio off-device are **disabled** on any surface that captures voice for a
  book.
- **Speech output on site.** Read-back uses the node's speech synthesis, or
  the browser's speech synthesis restricted to voices that run on the device.
- **Speech drafts; a spoken answer signs.**
  1. The transcript is parsed into a structured draft: first by the book's
     deterministic phrase grammar (the same phrases as its keyboard
     shortcuts), then by an on-site model if the grammar does not match.
  2. The draft goes through confirmation (ADR-144) and is read back.
  3. The person's spoken "confirm" is a signature. Nothing is signed because
     the entry itself was heard; only the answer to the read-back signs.
- **Voice is sufficient to sign, including acts that need a fresh sign-in.**
  Each person may enrol their voice. Speaker verification runs on the site
  node against the enrolment of the person signed in at that console.
  - A verified spoken "confirm" counts as a fresh authentication (`amr` =
    `voice`), so co-signatures, reliefs and seals can be completed by voice
    alone.
  - An unverified "confirm" (no enrolment, or verification below threshold)
    signs only what the console's current session may sign, and the record
    says so.
  - Enrolments are held on site, never leave it, and are revocable by the
    person.
- **Audio is evidence, retained by policy.** The attestation records the
  transcript and the audio checksum. The clip is kept or discarded per the
  book's retention policy (default: kept for the book's inspection window).
  People are told when audio is retained.
- **Wake models are our own.** Pretrained wake models under non-commercial
  licences are not used. Sites train or configure their phrase with tooling
  whose models we own or that are licensed ≤ Apache-2.0.

## Options considered

- **Browser Web Speech API** (what `useDictation` does today). Lost: audio
  leaves the device, and there are no wake phrases.
- **Always-on transcription with intent detection.** Lost: it transcribes
  everything said in an operations room, which is a privacy and records problem,
  and it raises false starts.
- **Push-to-talk only.** Kept as an option, but insufficient where hands are
  occupied; operators asked for voice specifically.
- **Commercial wake-word SDKs.** Lost on licence.

## Consequences

- appkit gains a `VoiceCapture` controller: wake detector, push-to-talk,
  recorder, indicators. Its transcription and speech endpoints are injected,
  as in `useDictation`. `useDictation` gains a mode that disables the Web
  Speech layer.
- The node gains a speech service (transcribe, synthesise), deployed with the
  node profile and built on one `axiom.speech` module. The signals voice-memo
  extractor, which imports Whisper itself today, moves onto that module, so
  there is one speech stack. It runs on CPU at console volumes.
- Wake-phrase false-accept and false-reject rates are measured per site in a
  noise sample from the real room, and the measurement is recorded before the
  wake phrase is enabled there.
- Speaker verification is part of the voice phase, not a later option,
  because voice alone signs. Its false-accept rate is measured per site with
  the same room recording as the wake word. A book may set a stricter
  threshold for its highest-consequence meanings.
- The agent's conversational turns ("How can I help?", "New entry?") are
  generated from the CLI vocabulary and the book declarations, not written
  per surface.
