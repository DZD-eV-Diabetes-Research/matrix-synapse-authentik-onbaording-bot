# ADR-0009 — E2EE stance: the bot operates outside encrypted rooms (no client-side crypto)

- **Status:** Accepted (2026-06-19) — answers BATTLE_PLAN §7 Q3
- **Context:** Group rooms are created with end-to-end encryption enabled for their members
  (G7.1, default on). The open question is whether **the bot itself** must participate in e2ee —
  i.e. decrypt/encrypt message events — which would force a crypto stack (libolm via
  `matrix-nio[e2e]`, now deprecated, or the `matrix-rust-sdk` crypto path) and persistent key
  storage.

## Decision

**The bot does not do client-side crypto.** It operates outside encrypted message flows:

- **Welcome/onboarding DMs are unencrypted.** They are created with the `trusted_private_chat`
  preset, which does **not** set `m.room.encryption`, so the bot's plaintext `m.text` messages
  send fine (G4.2).
- **In encrypted group rooms the bot only writes state** — power levels, name/topic, avatar,
  space child/parent, and the custom `onbot.*` bookkeeping events. State events are **not**
  encrypted, so the bot never needs to encrypt or decrypt anything there.
- **Drop the deprecated libolm path.** No `matrix-nio[e2e]`/libolm dependency, and no Olm key store
  is created. The `storage_dir` / `storage_encryption_key` config fields that backed it were removed
  in Phase 8 (they were unused); if encrypted-room support is ever revived, reintroduce them then.

## Rationale

- The bot's job (ADR-0001/0002) is identity→room projection, membership, power levels, and a
  plaintext welcome — none of which require reading encrypted message bodies.
- libolm is deprecated; adopting it for a capability we do not use is pure liability. This keeps
  ADR-0008 (no heavy Matrix library) coherent.

## Consequences

- The bot **cannot read or post message content inside e2ee rooms.** If a future feature needs
  that (e.g. the G4.6 admin broadcast delivered into encrypted rooms, or reading replies), this
  ADR must be revisited and the `matrix-rust-sdk` crypto path planned, with a persistent,
  optionally-passphrase-encrypted key store (G7.2).
- Welcome delivery must stay in unencrypted DMs; if a client auto-enables encryption on the DM,
  message sends will fail and onboarding for that user degrades to state-only — an accepted,
  visible failure mode rather than a silent one.
