# Decisions taken during the review

Maintainer decisions that shape the follow-up sessions. Date: 2026-11-04.

## D1 Leaked tokens in git history (SUP-01)

Both leaked `syt_` tokens (`@dzd-bot`, 2023, and a personal operator token, 2024) are known to be
invalid already, per the maintainer. No revocation step is planned. The BATTLE_PLAN Phase 1 rotation
item can be closed with that note.

## D2 History scrub (SUP-01)

The history will be scrubbed anyway, as its own session: `git filter-repo`, force-push of `main` and
all tags, re-created tags, GitHub Support request to purge cached commits. See the scrub plan in
`02-supply-chain.md`.

## D3 Room identity (REC-04, SEC-02, REC-13)

Rooms are tied to Authentik groups by a persisted mapping `group_pk -> room_id` (for group rooms,
lobbies, the space and the control room), not by canonical alias. The alias becomes presentation
only. Existing deployments need a one-time migration that adopts current rooms by alias plus the
onbot state marker, and then never looks them up by alias again.

## D4 Guard for destructive actions (REC-01, REC-02, LIF-08, MNT-01)

Three layers:

1. **Input plausibility.** An empty user or group list from Authentik aborts the pass. So does a drop
   of more than a configurable percentage against the last known good counts (persisted).
2. **Per-pass threshold for reversible destructive actions** (kick, MAS lock, room teardown). When a
   pass would exceed it, those actions are skipped, logged loudly and reported in the admin room.
3. **Confirmation for irreversible actions** (deactivate, erase) above a small number per pass: they
   wait for `!confirm` in the admin control room.

## D5 Legacy cutover (ONB-05)

The production cutover from the legacy bot has already happened. No adoption code. Only a short docs
note for other operators that a cutover creates fresh notice boards and re-sends the welcome.

## D6 `chatroom_params` (SEC-05)

Stays as it is: on by default, unvalidated. Whoever may edit group attributes in Authentik is trusted.
Only sharpen the docs (name the concrete consequences: `additional_creators`, public rooms,
`power_level_content_override`). One small exception that is a robustness fix, not a policy change:
a room the bot cannot fully manage (for example an extra creator it cannot kick) must not abort the
whole pass (covered by the per-room error isolation, REC-03).

## D7 Erase semantics (LIF-11)

New option `gdpr_erase_on_delete`, default `false`, applied identically by both effectors: MAS gets
`{"skip_erase": not gdpr_erase_on_delete}`, the Synapse admin path gets `erase=gdpr_erase_on_delete`.
Media deletion stays a separate switch.

## D8 Identity across renames (SEC-07, MNT-13)

Renames in Authentik (marriage, name change) must be supported: the person keeps their existing
Matrix account, because MAS fixes the localpart at first login. Consequence: identity is anchored on
the immutable Authentik user (`pk`/`uid`), not on the mutable username attribute.

Proposed design, to confirm at the start of that session:

- With `mas_admin` configured, resolve each Authentik user to the real MXID through MAS (upstream
  OAuth link by subject, then the MAS user's localpart). This is authoritative.
- Persist the binding `authentik pk -> MXID` (first seen) and keep using it when the attribute value
  changes. Without MAS this binding is the only source after the first sight.
- Report binding changes and duplicate MXIDs in the admin room; duplicates fail closed (the affected
  users are skipped, never merged).
- One function computes the MXID (today it is written four times, MNT-13).

## D9 Users who leave the sync scope during offboarding (S1)

A user who still exists in Authentik but is no longer in the sync scope while onbot has them locked or
in the offboarding pipeline is frozen: no unlock, no further destructive step. The bot reports it in
the admin room and an admin decides manually. If the user comes back into scope as active, the normal
re-enable applies.

## D10 Withdraw power levels (S2, REC-08, G8.4)

When a user loses the group membership that granted an elevated power level, the bot withdraws it.
It withdraws only entries it set itself (tracked by the bot); levels set by humans stay untouched.

## D11 Avatar fetching (S13, SEC-03)

Private, loopback and link-local destinations are blocked by default; a config allowlist can release
specific internal hosts. Plus a size cap, a total timeout, and redirects re-checked against the same
rules.

## D12 CI dependency updates and formatting (S15)

Dependabot for GitHub Actions, the Docker base image and Python dependencies. CI only checks
`ruff format` and fails; it no longer commits formatting fixes to `main`.
