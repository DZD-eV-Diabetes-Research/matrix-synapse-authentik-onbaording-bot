# ADR-0010 — The admin control room is a bounded exception to "reconcile, don't react"

- **Status:** Accepted (2026-07-10)
- **Context:** Operators need a way to address every user at once (GOALS G4.6). The bot already owns
  a read-only notice-board DM with each of them (ADR-0009, `onboarding/notice_board.py`), so the
  delivery mechanism exists. What did not exist is a way to *trigger* it from inside Matrix.

## Decision

Add an admin control room: one unencrypted, unfederated Matrix room, found by a configured alias, in
which an allowlisted set of administrators send prefixed commands (`!announce`, `!help`, `!status`)
that the bot reads off the sync stream and acts on.

It is **off by default** (`admin_room.enabled: false`). The same capability is available without it,
as `onbot broadcast`, for operators who would rather keep the trigger on the command line.

## Why this does not contradict ADR-0002 or ADR-0003

ADR-0002 says Authentik→Matrix sync is a convergence problem, and that any event is only a trigger to
run the same idempotent reconcile sooner. ADR-0003 carves out onboarding as event-driven, and that
carve-out is narrow: it reacts to *membership* events, and only to run a flow that is itself
idempotent.

A bot that reads `m.room.message` events and performs an action because of what they say is a real
departure from both. It is admitted here on these terms, and it is worth being precise about why they
are enough:

- **The control room is an operator interface, not a state source.** No command mutates the desired
  state. Nothing said in the room changes what the reconciler computes on its next tick; delete the
  room and the bot converges identically. The reconciler remains the sole authority on desired state,
  and Authentik remains the sole source of it.
- **It converges nothing, so it must not be idempotent — it must be *exactly-once-ish*.** This is the
  inversion that makes the room dangerous, and it is why the guards look nothing like the rest of the
  codebase. Everywhere else the bot re-runs a flow freely because re-running converges to the same
  state. `!announce` is not convergent: running it twice sends two messages to every employee. The
  sync stream *does* replay (the pump starts at `pos=None`; the server returns up to 50 timeline
  events per room), so the handler carries its own replay protection — an origin-timestamp floor at
  process start, plus a bounded ring buffer of handled event ids persisted in account data — and
  commits an event as handled *before* executing it. At-most-once, chosen deliberately over
  at-least-once: a lost announcement is an inconvenience, a repeated one is a company-wide page.
- **The blast radius is bounded by an allowlist, not by Matrix.** Authorisation is the sender's MXID
  against `admin_room.admin_user_ids`, checked on every command. It is explicitly *not* the sender's
  power level in the room: a power level says what someone may do inside a room, and `!announce`
  reaches far outside it. The room's power levels (`events_default: 0`, everything governing at 100)
  are a fence that keeps members from reshaping the room; the allowlist is the gate. Defence in
  depth — someone who gets into the room still cannot command the bot.
- **Bare messages are inert.** Only the `!` prefix addresses the bot, so the room is also a place
  admins can talk, without a sentence about an outage announcing itself to the company.

## Consequences

- The bot now reads message content, which ADR-0009 permits only outside encrypted rooms. The control
  room is therefore created unencrypted, and `m.federate: false` keeps a room that commands the bot
  from being reachable off-server. Should the room ever need to be encrypted, this ADR and ADR-0009
  must be revisited together — the bot would be unable to hear it.
- `OnboardingListener` no longer owns the sync loop. Two features consume the same stream, so it
  moved to `onbot/sync.py` as `SyncPump`, which fans each slice out to registered handlers.
- The allowlist is maintained by hand. Deriving it from Authentik superusers is tempting — the
  reconciler already computes `is_superuser` — but a capability reaching every user on the server
  should not silently widen because somebody was granted an unrelated role upstream. Noted as a
  possible future addition in the config field's description; deliberately not built.
- A future debug room (BATTLE_PLAN, deferred) can reuse `onbot/rooms/admin.py` for its shape. It must
  not reuse the command router: an admin room that reads and one that only writes have very different
  risk profiles.
