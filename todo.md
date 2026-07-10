# Onbot — planned work

Two self-contained work packages, each written as a cold-start prompt for its own session. They
touch disjoint files and can be done in either order, though **Session A first** is recommended:
it settles what the per-user bot room *is* (a read-only notice board), which is the thing Session B
broadcasts into.

Read `docs/adr/` and `docs/project/BATTLE_PLAN.md` before starting either one — this codebase has
explicit architectural decisions and both packages brush against them.

---

## Session A — the bot's DM room becomes a read-only notice board

Covers the old todo items *"Bot room read only for users"* and *"User needs to accept bot room
invitation — can we force the user into the room?"*. They are one change: both rewrite the same
`createRoom` call and the same `DirectRoomState` schema, and both need the same migration for rooms
that already exist.

### Where things stand

`WelcomeService._ensure_direct_room()` (`onbot/onboarding/welcome.py`) calls
`ApiClientMatrix.create_direct_message_room()` (`onbot/clients/matrix.py`), which creates the room
with `preset: trusted_private_chat` and an invite. The bot records the room in its own `m.direct`
account data, keyed by MXID. The user must accept the invite, and once they do they are a room admin
who can post, kick the bot, and change the topic.

`ApiClientSynapseAdmin.add_user_to_room()` (`onbot/clients/synapse_admin.py`) already wraps
Synapse's admin join API and is already used by the reconciler for the space and for group rooms.

### What to build

**1. New DM rooms are created correctly.**

Change `create_direct_message_room` to use `preset: private_chat` and pass a
`power_level_content_override` on `POST /createRoom` giving `users_default: 0`, `events_default: 50`,
`state_default: 50`, `invite: 50`, `kick: 50`, `ban: 50`, `redact: 50`. The bot ends up at 100 and
the user at 0, so Element shows a "you do not have permission to post in this room" banner in place
of the composer. Keep `is_direct: true` and keep the `invite`, since the invite is the fallback when
force-join is disabled or fails.

Build the power-level content in a small pure function so it can be unit-tested without a server.

**2. The user is force-joined.**

After `create_direct_message_room` returns, call `admin.add_user_to_room(room_id, mxid)`. This works
on an invite-only room because the calling admin (the bot) is in the room and may invite. `WelcomeService`
does not currently hold a `ApiClientSynapseAdmin` — thread one in from `build_app()` in `onbot/app.py`.

Gate it on a new config flag (`force_join_onboarding_room`, default `true`), and fall back to leaving
the plain invite standing if the join returns 403/404 — log a warning, do not raise.

Join **exactly once, at room creation**, and record it in `DirectRoomState` as `force_joined_at:
float | None`. Do not re-join on subsequent welcome calls: a user who deliberately leaves the room
must not be dragged back in on every reconcile tick.

**3. Give the room an identity.**

Force-joining skips the invite, and the invite is what makes Element tag a room as a DM (the bot can
write its *own* `m.direct` account data but not the user's). Without a name the room appears in the
user's normal room list as an untitled room with a bot in it. So set `name` and `topic` at creation,
from new config fields with sensible defaults, and reuse the existing avatar plumbing
(`MediaUploader` / `set_room_avatar`) if `synapse_server.bot_avatar_url` is set.

**4. Existing rooms: a migration command, because they cannot be fixed in place.**

`trusted_private_chat` gave every existing DM user power level 100, the same as the bot. Matrix
forbids changing a power-level entry greater than or equal to your own, so **the bot cannot demote
them**, and it cannot raise itself past 100 because nobody outranks it. Synapse's `make_room_admin`
does not help; it also tops out at 100. Do not waste time trying — verify the rule in the spec and
move on.

The only migration is to destroy and recreate. Add `onbot recreate-dm-rooms` to `onbot/cli.py`:

* read the bot's `m.direct` account data;
* for each room, read `m.room.power_levels` and select the rooms where the user's level is >= the
  bot's — those are the legacy ones. Skip the rest, so the command is idempotent and re-runnable;
* `--dry-run` (default) prints what it would do; require an explicit `--yes` to act;
* for each selected room: `admin.delete_room(room_id, purge=True)`, drop the `m.direct` entry, and
  let the normal welcome flow recreate it on the next reconcile.

Recreation re-sends the welcome messages, because the per-message content hashes in
`welcome_messages_sent` live in the state of the room being destroyed. That is expected. Say so in
the command's help text and in the docs — an operator running this on a live server will page every
user on it.

**5. Self-healing.**

`welcome_user()` already reads the DM's onbot state event on every call. Along that path, also read
`m.room.power_levels` and re-apply the desired content when it has drifted, skipping the write when
it already matches. This is cheap (one state read the flow was making anyway) and it repairs a room
where somebody hand-edited the levels — but it can only repair rooms where the bot still outranks
the user, i.e. rooms created after this change.

### Definition of done

* New config fields, with `title`/`description`/`examples` metadata, then `./gen_config_docs.sh` to
  regenerate `docs/CONFIG_REFERENCE.md` and `config.example.yml`. Never hand-edit those two.
* Unit tests: the power-level content builder; force-join fires once and is recorded; a room already
  carrying `force_joined_at` is not re-joined; force-join failure degrades to invite; the migration's
  room-selection predicate.
* Integration tests against the live stack (`./run_integration_tests.sh`) for force-join and for a
  PL-0 user actually being unable to send.
* A note in `docs/troubleshooting.md` about pre-existing rooms staying writable until migrated.

### Decisions already made

* Force-join on by default. The room is a notice board; consent to the notice board is consent to
  employment, not to a Matrix invite.
* Migration is opt-in and destructive, never automatic.

---

## ~~Session B~~ — done: `onbot broadcast` and the `#onbot-admin` control room

**Shipped.** See `docs/adr/0010-admin-control-room.md`. Three commits: the broadcast CLI, the
`SyncPump` refactor, then the control room. The section below is kept as the record of what was
asked for; the one deviation is noted here rather than silently: `!status` needed a last-reconcile
timestamp, so `ReconcilerEngine` gained a `last_reconcile_at` attribute set at the end of each pass.

Covers the old todo item *"come up with an idea how an admin can send a message on the bot's behalf
to all users' bot rooms"*. Two commits: the CLI first, because it needs no new machinery and is
independently shippable; the control room second, on top of the same service.

The control room is deliberately built as **general admin surface**, not as a broadcast feature with
a room bolted on. Broadcast is its first command; there will be others.

### Commit 1 — `BroadcastService` and the CLI

The bot's `m.direct` account data *is* the list of every user's bot room. That is the fan-out target,
and it needs no new bookkeeping.

Add `onbot/admin/broadcast.py` with a `BroadcastService` that reads `m.direct`, sends the message to
every room as `m.notice` (the convention for bot-originated messages; `send_text_message` in
`onbot/clients/matrix.py` currently hardcodes `m.text` — generalise it with a `msgtype` parameter
rather than adding a near-duplicate method), and returns a result object carrying the counts of rooms
sent, rooms failed, and the failures themselves.

Then `onbot broadcast "Maintenance at 22:00 UTC"` in `onbot/cli.py`. No authorisation logic is needed
or wanted: anyone who can exec into the container can already read the bot's access token.

Synapse rate-limits message sends per user, so a fan-out across a few hundred rooms will start
returning 429. Three things:

* the shared `BaseApiClient` already retries 429 via tenacity — check `RETRYABLE_STATUS_CODES` in
  `onbot/clients/base.py` and confirm the backoff is sane for this, rather than assuming;
* bound the send concurrency (a semaphore, not an unbounded `gather`);
* on startup, best-effort call Synapse's `override_ratelimit` admin endpoint for the bot user
  (`POST /_synapse/admin/v1/users/<user_id>/override_ratelimit` — verify the verb against the Synapse
  admin API docs before implementing). Never let its failure block startup; the bot works without it.

### Commit 2 — the control room

**The sync refactor comes first.** `OnboardingListener.run()` in `onbot/onboarding/listener.py` owns
the sliding-sync loop today and only extracts `m.room.member` events. The control room needs
`m.room.message` events off the same stream, and opening a second sync connection would be wrong.

Lift the loop into `onbot/sync.py` as a `SyncPump` that owns `_pos`, the error backoff and the stop
event, and fans each `SyncResult` out to registered handlers. `OnboardingListener` loses `run()` and
gains `handle_sync(result)`; the command router becomes a second handler; `build_app()` in
`onbot/app.py` constructs the pump and registers both. This is a small refactor (~60 lines moved) but
do it as its own commit so the diff is legible.

**Replay protection is not optional.** The pump starts with `_pos = None`, and sliding sync then
returns up to 50 timeline events per room. Onboarding tolerates this because welcoming is idempotent.
A broadcast handler that tolerates it would re-send the last announcement to every user on every bot
restart. Two guards, both:

* ignore events whose `origin_server_ts` predates process start;
* dedupe on `event_id` against a bounded ring buffer persisted in the bot's account data (namespace
  the type with the existing `event_type_name()` helper from `onbot/reconciler/state.py`, e.g.
  `org.company.onbot.admin_cursor`).

Write a unit test that feeds the same `SyncResult` twice and asserts one send.

**The room itself.** Add `onbot/rooms/admin.py`: ensure-if-missing by alias (`#onbot-admin:server`,
configurable), marked with a new `OnbotRoomType.admin_room` state event so it is recognisably
bot-managed, unencrypted (ADR-0009), created with `m.federate: false`, and with power levels that let
the bot (100) manage state while members can talk (`events_default: 0`, `state_default: 100`,
`invite: 100`, `kick: 100`). Invite the admins — no force-join, they are humans.

**Who is an admin.** An explicit MXID allowlist in config (`admin_room.admin_user_ids`). Deriving it
from Authentik superusers is tempting — the reconciler already computes `is_superuser` — but an
allowlist is the safer default for a capability that reaches every user on the server. Note the
derived option in the config field's description as a possible future addition; do not build it now.

Authorise on the **sender MXID against that allowlist**, not on the sender's room power level. If
someone gets themselves into the control room, power level alone should not be the only thing between
them and a message to the entire company. Defence in depth: the allowlist check is the gate, the
power levels are the fence.

**The command router.** A bare message with no command prefix is ignored, so admins can discuss in
the room without accidentally paging everyone. `!announce <text>` fans out via `BroadcastService`;
the bot replies in the control room with `sent to 42 rooms, 1 failed` and names the failures. Also
add `!help` (prints the same text as the pinned message) and `!status` (bot version, last reconcile
time, number of managed DM rooms). Keep parsing in a pure function with its own unit tests — do not
regex inside the handler.

Ignore events sent by the bot itself, or the reply to `!announce` will be re-parsed.

**Pinned documentation.** On startup, post a help message listing the available commands and pin it
via the `m.room.pinned_events` state event. `send_text_message` returns the event id, so pinning is a
`put_room_state_event` away. Make it idempotent: store a hash of the help text in the room's onbot
state event and only re-post + re-pin when the text changes — otherwise every restart posts another
copy. This mirrors the per-message hashing that `welcome.py` already does for welcome messages; read
that first and follow the same shape.

Set the room topic to a one-line version of the same thing, so it is visible without scrolling.

### Definition of done

* Config: `admin_room` block (enabled, alias, name, topic, `admin_user_ids`), with generated docs.
* Unit tests: command parsing; allowlist authz (including a non-admin in the room being refused);
  the replay/dedupe guard; the pinned-help idempotency hash; the broadcast fan-out over a fake client.
* Integration test: an allowlisted admin's `!announce` lands in a provisioned user's DM room, and a
  non-allowlisted member's does not.
* **An ADR.** A bot that reads room messages and acts on them is a real departure from ADR-0002
  ("reconcile, don't react") and reaches past the narrow event-driven carve-out of ADR-0003. Write
  `docs/adr/0010-admin-control-room.md` explaining why the control room is a bounded exception:
  it is an operator interface, not a state source, and it converges nothing.

### Decisions already made

* CLI first, control room second, sharing one `BroadcastService`.
* Explicit MXID allowlist, not derived from Authentik.
* Commands are prefixed (`!announce`); bare chat in the room is inert.
* Announcements go out as `m.notice`.

---

## Deferred

* **Debug room** — a room where admins watch onbot's log messages. Shelved deliberately, not
  forgotten. The mechanics are easy (a `logging.Handler` feeding a bounded queue, drained by a task
  that batches records into `m.notice` messages); the hazards are that the handler must never do I/O
  inline, must not feed itself (posting logs through `httpx`, which logs, which posts), and that at
  `DEBUG` the bot logs every API call it makes — including bearer tokens — into a plaintext,
  unencrypted room that admins read on their phones and that cannot be un-synced once leaked. If this
  comes back: default the room's level to `WARNING`, and reuse `onbot/rooms/admin.py` from Session B.
