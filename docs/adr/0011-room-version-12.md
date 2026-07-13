# ADR-0011 — Room version 12 readiness: creator-as-top-of-lattice, domainless room IDs

- **Status:** Accepted (2026-07-13)
- **Context:** The Matrix spec now says servers SHOULD use **room version 12** as the default when
  creating rooms ([spec/rooms/v12](https://spec.matrix.org/latest/rooms/v12/)). A current Synapse
  therefore hands the bot v12 rooms — through federation, through rooms an admin made by hand, and
  through the bot's own `createRoom` calls, which inherit the server default. The bot had code that
  silently assumed the older room shape. This ADR records the two v12 assumptions the bot now makes,
  so the next person to touch power-level or room-ID code does not reintroduce the old bug.

## What v12 changes that this bot depends on

1. **Room creators hold an infinite, immutable power level and are absent from
   `m.room.power_levels`.** The sender of `m.room.create` — plus anyone in the create event's
   `additional_creators` array — outranks every explicit power level and **cannot be named in an
   `m.room.power_levels` event at all**: the auth rules reject a power-levels event that lists a
   creator in its `users` map. The bot is the creator of every room it makes, so the bot is no longer
   in the `users` map of its own rooms.

2. **Room IDs are a hash of the create event, with no `:domain` component**
   (e.g. `!Nhcu5BS-UMnFX7hBVfVSoXiD7OgH6iRT-xyIuqDnpYQ`). Room *aliases* (`#name:server`) and MXIDs
   (`@user:server`) still carry a domain; room *IDs* do not.

## Decision

**The single rule: a creator sits at the top of the power lattice and is absent from
`m.room.power_levels` by design. Absent ≠ powerless.** Enforced everywhere:

- **Writing power levels.** The `power_level_content_override` the bot passes to `createRoom` for the
  notice-board DM (`onbot/onboarding/notice_board.py`) and the admin control room
  (`onbot/rooms/admin.py`) omits the `users` map entirely. On v12 the creator must not be named there;
  on older versions the server's default power levels seat the creator at 100 in `users` for us.
  Either way the bot governs the room without being listed. It is **wrong** to write `users: {bot:
  100}` (v12 rejects it) and equally wrong to write `users: {}` (on v11 that demotes the creator to
  `users_default`) — so we omit the key and let the server seat the creator.
- **Reading power levels.** `legacy_user_matches_or_outranks_creator()`
  (`onbot/reconciler/power_levels.py`) is the one predicate for "is this user ≥ the bot here?". When
  the creator is absent from `users` it returns `False` unconditionally (the creator is infinite; no
  one matches it). Only on a pre-v12 room, where the creator is an ordinary `users` entry, does it
  compare levels. Reading an absent creator as `users_default` — the naive pre-v12 assumption — would
  flag every healthy v12 room as broken.
- **Room IDs are opaque tokens.** Never construct a room ID from parts and never split one on `:` to
  recover a server name — there is none. `build_canonical()` (`onbot/identity.py`) is for user IDs and
  room *aliases* only; its `!` sigil was removed. The one `:`-splitter in the codebase,
  `mxid_localpart()`, operates on MXIDs, which keep their domain, and must never see a room ID.
- **No room version is pinned in code.** `synapse_server.room_version` is a single server-wide config
  field, unset by default so rooms inherit the server's own default (now v12). It exists only for an
  operator whose Synapse is too old, or a test forcing a specific version. Pinning a number would
  freeze the bot behind the ecosystem.

## Consequences

- **The destructive `recreate-dm-rooms` migration (Session A) shrinks to a one-shot cleanup.** The
  failure it repairs — a DM user stuck at power level 100, equal to the bot, whom the bot cannot
  demote — **cannot happen in any room created as v12**, because the creator's power is unmatchable by
  construction. `legacy_user_matches_or_outranks_creator()` returns `False` for every v12 room, so the
  migration selects only pre-cutover rooms.
- **The integration stack runs v12.** The test Synapse is pinned to a release that supports v12 and
  configured with `default_room_version: "12"` (`tests/integration/stack/synapse/homeserver.yaml`), so
  the suite exercises the world the bot now runs in rather than a v9/v11-defaulting server.
- **Older room versions still work.** Omitting `users` from the override and the creator-aware read
  predicate are both correct on pre-v12 rooms too, so an operator on an older Synapse (via
  `room_version`, or an old server default) is unaffected.
