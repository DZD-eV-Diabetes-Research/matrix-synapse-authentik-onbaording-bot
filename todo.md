# Onbot — planned work

Two self-contained work packages, each written as a cold-start prompt for its own session. They
touch disjoint files and can be done in either order, though **Session A first** is recommended:
it settles what the per-user bot room *is* (a read-only notice board), which is the thing Session B
broadcasts into.

Read `docs/adr/` and `docs/project/BATTLE_PLAN.md` before starting either one — this codebase has
explicit architectural decisions and both packages brush against them.

**Do Session R (room-version readiness) first.** It is foundational: Session A's migration and
Session D's lobby both depend on the bot reading power levels and room IDs correctly under the room
version current Synapse now creates by default.

---

## Session R — make the whole bot room-version-12 ready

Not tied to any one feature. This is due because the ecosystem moved: the Matrix spec now says servers
SHOULD use **room version 12** as the default when creating rooms, so a current Synapse already hands
the bot v12 rooms — through federation, through rooms an admin made by hand, and through the bot's own
`createRoom` calls, which inherit the server default. The bot has code that silently assumes the older
shape. This session finds and fixes all of it, once, for the whole project.

Verify every fact below against the spec before coding — `https://spec.matrix.org/latest/rooms/v12/`
and the room-versions index — rather than trusting this summary.

### What v12 changes that the bot cares about

1. **Room creators have an infinite, immutable power level, and do not appear in
   `m.room.power_levels`.** The user who sent `m.room.create`, plus anyone in the create event's
   `additional_creators` array, outranks every explicit power level and cannot be named in a
   `m.room.power_levels` event at all — the authorization rules reject such an event. Consequence for
   this bot, which is the creator of every room it makes: **the bot is no longer in the `users` map of
   its own rooms.** Any logic that fetches the bot's own level from `power_levels` and compares now
   reads the bot as absent, i.e. as `users_default` (typically 0), and draws the wrong conclusion.

2. **Room IDs are now the hash of the create event, with no `:domain` component**
   (e.g. `!Nhcu5BS-UMnFX7hBVfVSoXiD7OgH6iRT-xyIuqDnpYQ`). Any code that splits a room ID on `:` to
   recover a server name breaks. Room *aliases* still carry a domain (`#name:server`); room *IDs* no
   longer do. The distinction matters — fixing the wrong one reintroduces the bug.

3. State resolution iterated again (v2.1). Not bot-visible; note it and move on.

### The audit — the whole codebase, not one module

* **Every read of `m.room.power_levels`.** Start from `onbot/clients/matrix.py`
  (`get_power_levels`/`set_power_levels`) and follow callers: the reconciler's power-level pass
  (`onbot/reconciler/power_levels.py`), the admin/notice-board room provisioners
  (`onbot/rooms/admin.py`, `onbot/onboarding/notice_board.py`), and Session A's planned DM self-heal
  and `recreate-dm-rooms` predicate. Anywhere the code asks "what is the bot's level here?" or "is this
  user's level ≥ the bot's?", teach it that a creator (the bot, and anyone in `additional_creators`)
  sits above the lattice and is absent from the map by design — not that an absent user is powerless.

* **Every parse of a room ID.** Grep for `":"` splits and `.split(":")` over values that are room IDs
  (as opposed to aliases or MXIDs, which keep their domain). Begin at `onbot/identity.py`, which
  documents `!` → room/space ID, and check `onbot/models.py` (`MatrixRoom`) and anywhere a room ID is
  logged, keyed, or compared by structure.

* **The integration stack's Synapse.** Confirm which room version the pinned Synapse in the test
  compose file actually defaults to, and bump it so the integration tests exercise v12. A green suite
  against a v9-defaulting Synapse proves nothing about the world the bot now runs in.

* **`createRoom` room-version handling.** Add one server-wide config field for the room version, unset
  by default so rooms inherit the server's own default (now v12). It exists for the operator whose
  Synapse is too old, and to let a test force a specific version. Do **not** pin a number in code —
  pinning freezes the bot behind the ecosystem.

### The opportunity — say this out loud, it changes Session A

Once the bot is the infinite-power *creator* of the rooms it makes, the failure mode that Session A's
entire destructive `recreate-dm-rooms` migration exists to repair — a DM user who ended up at power
level 100, equal to the bot, whom the bot then cannot demote — **cannot happen in any room created
after the cutover.** The creator's power is unmatchable by construction. Session A's migration does not
disappear (rooms made before the cutover, under `trusted_private_chat`, still need it) but it shrinks
from a permanent feature to a one-shot cleanup. Sequence Session R before Session A so Session A can be
written against the smaller problem.

### Definition of done

* One config field for the server-wide room version, with `title`/`description`/`examples`, unset by
  default, then `./gen_config_docs.sh`. Never hand-edit `docs/CONFIG_REFERENCE.md` or
  `config.example.yml`.
* Every `power_levels` read and every room-ID parse identified in the audit fixed, each with a unit
  test over a synthetic v12 input: a `power_levels` content with the bot **absent** from `users`
  (asserting the bot is still treated as authoritative), and a v12 room ID with **no** `:domain`
  (asserting nothing tries to extract a server name from it).
* The integration Synapse bumped to a v12-defaulting release, and the existing suite green against it.
* A short ADR or an amendment to an existing one recording the v12 assumptions the bot now makes —
  creator-as-top-of-lattice and domainless room IDs — so the next person to touch power-level code
  does not reintroduce the "absent means powerless" bug.

### Decisions already made

* No room version is pinned in code anywhere; the version is a config field that defaults to the
  server's own default.
* "Creator" is the top of the power lattice and is absent from `power_levels` by design; absent ≠
  powerless. This is the single rule the audit enforces everywhere.
* Session R precedes Session A, so Session A's migration is scoped to pre-cutover rooms only.

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
* *(Revised by Session C: the allowlist may now also be sourced from an Authentik group.)*

---

## ~~Session C~~ — done: bot admins may also come from an Authentik group

**Shipped.** `onbot/admin/admins.py` holds the pure `resolve_admin_mxids()` and the `AdminResolver`
that caches it; `ControlRoomHandler` and `AdminRoomProvisioner` both take the resolver instead of
reading `admin_user_ids` themselves. The ADR carries a dated amendment. The section below is kept as
the record of what was asked for; the one open choice is resolved here rather than silently: the
refresh TTL **reuses `server_tick_rate_sec`** rather than adding a knob, on the grounds that it is
already the operator's answer to "how stale may the bot's view of Authentik be?".

Sessions A and B are merged. This is a follow-up to Session B, and it partially reverses one of its
decisions, so read `docs/adr/0010-admin-control-room.md` first — you will be amending it.

### Where things stand

`admin_room.admin_user_ids` in `onbot/config.py` is a hand-maintained list of MXIDs. It is the only
gate on `!announce`, a command that writes into every user's room on the server. Two places read it,
and **both snapshot it once**: `AdminRoomProvisioner._ensure_admins_invited()` (`onbot/rooms/admin.py`)
iterates it on startup, and `ControlRoomHandler.__init__` (`onbot/admin/control_room.py`) freezes it
into `self.admins`, which `_handle_message()` then checks the sender against.

Keeping the list by hand is the friction we are removing. Authentik already knows who the
administrators are, and `ApiClientAuthentik.list_users(filter_groups_by_pk=...)` already returns a
group's members. `onbot.identity.compute_mxid()` already maps an Authentik user to the MXID their
account will have under MAS — it is the same mapping the reconciler uses, so it cannot drift.

### What to build

**1. A second source for the allowlist.**

Add `admin_room.authentik_group_pks_granting_bot_admin: list[str]`, defaulting to `[]`. Members of
any listed Authentik group may command the bot. The effective admin set is the **union** of that and
the existing `admin_user_ids`, which stays and is still the right home for Matrix-only accounts that
Authentik has never heard of (a break-glass admin, another bot).

`admin_user_ids` keeps its meaning; only its docstring changes, since it is no longer the only
source. The generic name — `admin_authentik_group_pk` — was rejected on purpose: the field says what
membership *grants*, because a reader of `config.yml` needs to see the capability, not the plumbing.

**Do not add a fallback to Authentik superusers.** An empty union means *nobody may command the bot*;
the control room is still created and every command is refused with the existing non-admin reply.
This is the whole point: `!announce` reaches every employee, and people are made Authentik
superusers to administer an identity provider, not to page the company. A superuser fallback would
make the most dangerous capability in the bot the implicit default, and would silently extend it to
whoever is granted superuser next month. Say this in the config field's description, and record it in
the ADR amendment below — otherwise somebody will helpfully add the fallback back.

**2. Resolution is pure; fetching is not.**

Add `onbot/admin/admins.py` with a pure `resolve_admin_mxids(config, group_members) -> frozenset[str]`
taking the raw Authentik user dicts and returning the union. It must:

* map each member with `compute_mxid()`, using the same `authentik_username_mapping_attribute` the
  reconciler uses. `compute_mxid` raises `KeyError` on an unmappable user — **catch it, log a
  warning, and drop that user.** A user we cannot map deterministically must never be granted
  anything, and must never take the bot down either;
* drop users on `authentik_user_ignore_list` (matched on the raw `username`, as everywhere else). A
  service account someone parked in the admin group is not an admin. `matrix_user_ignore_list` does
  *not* apply here — those are the Matrix-only accounts `admin_user_ids` exists for;
* leave inactive users out — `list_users` already defaults to `filter_is_active=True`, so let it, but
  assert it in a test so a change to that default cannot silently re-admit a disabled account.

Around it, an `AdminResolver` holding the Authentik client and a cached set, refreshed on demand.

**3. The set is now dynamic, and that is the hard part.**

Removing someone from the Authentik admin group must revoke their command access **without a bot
restart** — otherwise this is worse than the hand-maintained list it replaces, because it *looks*
revocable. So `ControlRoomHandler` may no longer freeze `self.admins` at construction: it consults
the resolver when authorising, and the resolver refreshes on a TTL (a config field, or reuse
`server_tick_rate_sec` — pick one and say why in a comment).

Authorise against a set that is at most one TTL stale. Do not authorise against a set that is one
process-lifetime stale.

Note in passing what this does *not* do: a demoted admin stays in the control room and can still
read it. Kicking them is a separate decision. Do not make it here; leave a comment saying so.

`AdminRoomProvisioner` invites the resolved union too, so a new group member finds the room waiting.
It runs on startup only, which is fine — an admin added later is invited on the next restart, and can
be invited by hand meanwhile. Do not turn the provisioner into a loop for this.

**4. A failure to reach Authentik must not open the gate.**

If the refresh raises, keep the previous set and log — never fall back to an empty set that refuses
everyone (a self-inflicted outage of the control room) and never to a permissive one. On the very
first resolution, before any successful fetch, the set is `admin_user_ids` alone: the explicit list
is the floor, and it does not depend on Authentik being up.

### Definition of done

* Config: the new field with `title`/`description`/`examples`, an updated `admin_user_ids`
  description, then `./gen_config_docs.sh`. Never hand-edit `docs/CONFIG_REFERENCE.md` or
  `config.example.yml`.
* Unit tests: the pure resolver (union; unmappable user dropped, not fatal; ignore-listed user
  dropped; empty union); a group member gaining and then **losing** command access across a refresh,
  without reconstructing the handler; a failed refresh preserving the previous set; the explicit
  `admin_user_ids` working with Authentik unreachable.
* Integration test against the live stack (`./run_integration_tests.sh`): an Authentik user in the
  configured group runs `!announce` and it lands in a provisioned user's welcome room; the same user,
  removed from the group, is refused after a refresh.
* **Amend `docs/adr/0010-admin-control-room.md`.** Its "Decisions" section says the allowlist is
  explicit and not derived from Authentik; that is now half-wrong. Do not rewrite history — add a
  dated amendment recording that an Authentik *group* is an accepted second source (it is still an
  explicit opt-in: somebody must create the group and put people in it), that deriving from
  *superusers* remains rejected, and why the two are not the same argument.

### Decisions already made

* Union of the two sources; no superuser fallback; empty union means no commands.
* Authorisation is re-resolved on a TTL, not frozen at startup.
* A user removed from the group loses commands but keeps their seat in the room.

---

## Session D — lobby rooms: an open front door in front of a closed group room

The wanted semantics, in one sentence: the group room `#duesseldorf` stays exactly as private as it
is today, and beside it the bot maintains a second room — its **lobby** — which every member of the
parent space can find and join of their own accord, and which nobody is ever kicked out of.

This replaces an earlier draft of this section that proposed making the group room itself visitable.
That draft is not recoverable from this file, so the reason it was dropped is recorded here: opening
an existing room retroactively exposes its history. `history_visibility` defaults to `shared`, so in
an unencrypted group room the first visitor can scroll back to the day the room was created, through
conversations nobody held with visitors in mind. A lobby has no history to leak, because it starts
empty on the day it is created. That is the difference between a feature you can enable on a live
deployment and one you cannot.

### The visibility question, answered

*Does a non-Düsseldorf user see the lobby but not the group room?* **Yes**, and it falls out of
Synapse's existing space-hierarchy filter rather than needing code. When a client asks the space for
its children, Synapse includes a child room only if the asking user is joined or invited to it, or
its history is `world_readable`, or its join rule is `public` or `knock`, or its join rule is
`restricted` and the user is in one of the spaces named in the rule's `allow` list.

So with `#duesseldorf` left at `join_rule: invite` and `#duesseldorf-lobby` at `join_rule:
restricted` allowing the parent space, a space member who is not in the Düsseldorf group sees exactly
one of the two rooms, and it is the right one. A Düsseldorf group member is joined to both and sees
both. No new listing code, no new permission model.

**The caveat, which must go in the docs rather than be discovered later.** The room's *existence* is
not a secret, only its contents are. The `m.space.child` event the bot writes onto the space carries
the private room's ID, and space state is readable by any space member; likewise any user can resolve
`#duesseldorf:server` to a room ID through the directory. What a non-member cannot do is join it,
read its history, see its name or topic, or enumerate its members. Element will not show it to them.
A curious person with `curl` can learn that a room with that ID exists. If that is unacceptable the
answer is not this feature, it is a second space — say so plainly and do not try to paper over it.

*(Worth ten minutes during implementation: check whether Element still files a room under its space
when only the room's `m.space.parent` is set and the space's `m.space.child` is omitted. If it does,
private group rooms could skip the child event and stop leaking even their IDs. Treat this as
unverified — do not build on it without testing both Element Web and Element X.)*

### Naming the thing

The suffix is the entire user-facing explanation of what the second room is. It has to say *this is a
door*, not *these are more people*. `& Friends` and `& Visitors` were the first candidates and both
fail on that: the ampersand reads as a statement about membership ("Düsseldorf, and also some
friends"), which invites exactly the confusion where nobody knows which of the two rooms to post in.

Default to **`" (Lobby)"`**, alias suffix **`"-lobby"`** — `Düsseldorf (Lobby)` / `#duesseldorf-lobby`.
A lobby is unambiguously the room you pass through, it is understood in English and German alike, and
it carries no promise that anything happens there. Ship the suffix as a config field, obviously, and
name these alternatives in its description so the operator picks deliberately:

* `" (Foyer)"` — the same idea, reads more naturally to a German-speaking org, and is what a DZD
  reader would probably choose;
* `" (Open)"` — shortest, and says the property rather than the metaphor;
* `" & Guests"` — if you want the room to feel like the group hosting, not the group hiding.

Set the lobby's **topic** from a template, defaulting to something that states the arrangement in the
one place every visitor looks: *"Open lobby for the Düsseldorf group — anyone in the space may join.
The group's working room is private."* A visitor who reads that never wonders why the room is quiet.

Note for whoever implements the alias: `compute_room_attributes()` strips dashes
(`alias.replace("-", "")`, `onbot/reconciler/rooms.py:70`, legacy behaviour that preserves alias
matching). Append the lobby suffix **after** that call, so `-lobby` survives. Dashes are legal in
Matrix aliases; the stripping exists to keep the *group-derived* part stable, not because dashes are
a problem.

### What this buys, structurally

Nothing about the group room changes. It keeps its join rule, its history, its encryption, and its
membership — which stays a pure projection of the Authentik group, kicks and all. ADR-0002 survives
intact for it.

The lobby is **add-only**. Nobody is ever kicked from it. That is one line: `diff_room_membership()`
already takes `kick_enabled` as a parameter, and `ReconcilerEngine` currently hands it the global
`kick_matrix_room_members_not_in_mapped_authentik_group_anymore` for every room. Pass `False` for
lobbies. There is no ledger of bot-injected members, no `to_kick` intersection, none of the machinery
an in-place visitable room would have demanded.

"Except users removed from the whole system" needs **no code at all**. Removal from the system is
account deactivation (`onbot/lifecycle/accounts.py`), and Synapse's deactivation drops the account
from every room server-side. Verify this in an integration test rather than trusting this paragraph,
but do not implement it.

The consequence, which is correct and should be stated in the config docs: a user removed from the
Authentik `Düsseldorf` group is kicked from `#duesseldorf` and **keeps their seat in the lobby**,
where they are now indistinguishable from any other visitor. That is what a lobby is for.

### What to build

**Prerequisite 0: Session R (room-version readiness) is done.** The lobby's `restricted` join rule
needs the room-version floor Session R establishes, and the join-rule reconcile pass reads
`m.room.power_levels` to confirm the bot may write state — a read Session R makes v12-correct. Do not
start the lobby before Session R lands; it is written up as its own top-level section above.

**Two prerequisite bug fixes, each its own commit, before any of the feature.**

*1. `initial_state` is silently clobbered.* `ApiClientMatrix.create_room()`
(`onbot/clients/matrix.py`) builds its own `initial_state` list — space parent, encryption — then
assigns `body["initial_state"] = initial_state` *after* spreading `room_params` into `body`. Anything
an operator passes under that key is dropped without a warning. Merge the two lists, caller's events
last. The lobby needs its join rule as an `initial_state` event at creation, so this blocks the
feature; but it is a bug on its own and gets its own commit and its own test.

*2. Per-group room overrides discard the defaults.* `resolve_room_settings()`
(`onbot/reconciler/rooms.py`) merges with `defaults.model_dump() | override.model_dump()`. Pydantic
has already filled the override model with *class* defaults for every key the operator omitted, so
the right-hand side is complete and the merge throws `matrix_room_default_settings` away entirely.
The config field's description promises "only the keys you list are overridden"; the code does not do
that. It goes unnoticed today only because the class defaults and the configured defaults usually
coincide. Fix with `model_dump(exclude_unset=True)` on the override. Do this before adding a field
whose entire purpose is to be set per-group.

**3. Config.**

On `MatrixDynamicRoomSettings`, so that per-group overrides come for free through the existing
`per_authentik_group_pk_matrix_room_settings`:

Every field carries the `visitor_lobby_` prefix. `lobby_*` alone was rejected: standing in a config
file the word says nothing about *whose* lobby or *why*, and this whole file is about rooms. The
prefix names the intent — a room a *visitor* from the wider space may walk into — and echoes the
operator's own word for the feature ("visit Düsseldorf").

* `visitor_lobby_enabled: bool = False` — off by default. A lobby is a decision, not a migration.
* `visitor_lobby_name_suffix: str = " (Lobby)"`, `visitor_lobby_alias_suffix: str = "-lobby"`.
* `visitor_lobby_topic_template: str` — with `{name}` interpolated from the group room's name.
* `visitor_lobby_end2end_encryption_enabled: bool = False` — see below; this is a real default, not
  a copy of the group room's.
* `visitor_lobby_inject_group_members: bool = True` — see below.

Plus `matrix_room_visitor_lobby_from_authentik_attribute` (default `attributes.chatroom_visitor_lobby`),
so the person who owns the group in Authentik can open a lobby without a config deploy. That is the
ergonomic that decides whether this feature is used at all. Parse it as a bool, fall back to the
configured default on garbage, and log a warning naming the group — the same shape as the existing
create-params JSON path.

Reject `visitor_lobby_enabled` at config-validation time when `create_matrix_rooms_in_a_matrix_space` is
disabled. `restricted` needs a space to be restricted *to*. A lobby that silently stayed invite-only
would be worse than a startup error.

**4. The lobby's own defaults, and why they differ from the group room's.**

*Unencrypted, by default.* Encryption cannot be turned off once a room exists, and it buys a lobby
little: the room is open to the whole space by construction, so its threat model is the homeserver
admin, who is already the person running this bot. Meanwhile encryption guarantees every visitor's
first impression is a screen of "unable to decrypt". Let the operator turn it on; do not do it for
them. (Note honestly in the docs that this is a *weaker* default than the group room's, and why.)

*Power levels: visitors can talk.* A lobby where the visitor cannot speak is a notice board, and this
codebase already has one of those. Keep the preset defaults — `users_default: 0`, `events_default:
0` — with the bot at 100 and `state_default: 100` so only the bot rewrites room state.

*`m.space.child` with `suggested: true` for the lobby.* `create_room()` currently hardcodes
`suggested: True` for every room it files under the space. Make it a parameter: `true` for lobbies,
which are the rooms a wandering space member *should* be nudged toward, and `false` for private group
rooms, which they cannot join anyway.

**5. Join rules get a reconcile pass.**

Add `onbot/reconciler/join_rules.py`, shaped like the existing `power_levels.py`: a pure
`desired_join_rules(room_kind, space_id) -> dict | None`, and a diff against the room's current
`m.room.join_rules` content. Write only on difference — a no-op tick must send no state events. For a
lobby the content is

    {"join_rule": "restricted",
     "allow": [{"type": "m.room_membership", "room_id": "<space_id>"}]}

which is why this can never be expressed as static config: the space's room ID is resolved at runtime
from its alias. Pass the same content as `initial_state` at creation, so a lobby is never briefly
invite-only nor briefly open. Wire the pass into `ReconcilerEngine` beside the power-level pass, over
existing lobbies as well as new ones — that is what lets an operator close a lobby again by flipping
the flag, which a create-time-only parameter would not.

**Room version: do not pin a number here.** A `restricted` rule needs room version 8+ and
`knock_restricted` needs 10+; below that, `createRoom` accepts the rule and then silently ignores it,
permanently, unfixable without recreating the room. But pinning `"10"` (as an earlier draft of this
line did) freezes the bot at 10 forever while servers move on — the spec now says servers SHOULD
default to **v12**. Leave `room_version` unset so a lobby inherits the server default, and rely on the
single server-wide room-version config field introduced by the **v12-readiness prerequisite below**.
The only hard requirement this feature adds is a *floor* of 10, which any Synapse new enough to run
this stack already clears. Assert the floor; do not pin a ceiling.

The bot is PL 100 and `state_default` is 100, so it may send the event. Do not assume it: a room
somebody hand-edited will 403, and the pass must log and carry on to the next room rather than abort
the tick.

**6. The model learns about a second room.**

`GroupRoomMap` (`onbot/models.py`) is 1:1 group→room. Give it `lobby: MatrixRoom | None` and a second
`RoomCreateAttributes`. Add `OnbotRoomType.visitor_lobby` to `onbot/reconciler/state.py` and stamp the
lobby with it at creation, so discovery does not mistake a lobby for a group room, try to project
Authentik membership onto it, and kick every visitor on the next tick. **This is the single most
dangerous bug this session can produce.** Write the test that would catch it before writing the code
that would cause it.

`disable_rooms_when_mapped_authentik_group_disappears` must take the lobby with the group room when
the group vanishes. A blocked group room beside a live, joinable lobby is the worst of both.

**7. Membership.**

If `visitor_lobby_inject_group_members` is true — the default — the group's members are joined into the lobby
as well as the group room, so a visitor who walks in finds somebody there. This is the whole bet, and
it is a social one: it works when the group room is where the group *works* and the lobby is where
the group is *reachable*, and it fails, producing two half-dead rooms and doubled notifications, when
both are general-purpose. The suffix and the topic template are load-bearing here, not decoration.

The knob exists because a large group may prefer an empty lobby it staffs deliberately. It is not the
default: an empty lobby is a dead lobby.

Visitors are never injected anywhere. They joined on purpose.

### Definition of done

* Session R landed first (its own definition of done applies).
* The two prerequisite bug fixes, each with its own commit and its own regression test: a
  caller-supplied `initial_state` event surviving alongside the space-parent and encryption events; an
  override naming one key inheriting the rest from `matrix_room_default_settings` rather than from the
  class defaults.
* Config: the new fields with `title`/`description`/`examples`, the cross-field validator against
  `create_matrix_rooms_in_a_matrix_space`, then `./gen_config_docs.sh`. Never hand-edit
  `docs/CONFIG_REFERENCE.md` or `config.example.yml`.
* Unit tests: `desired_join_rules()` per room kind; the no-op tick sending no state event; the lobby
  alias keeping its dash through `compute_room_attributes()`; an invalid Authentik attribute value
  falling back to the configured default; **a lobby room never appearing in the group-room membership
  diff**; `kick_enabled=False` on a lobby whose members are not in the group.
* Integration test against the live stack (`./run_integration_tests.sh`), asserting all four halves of
  the promise: a space member outside the Düsseldorf group **sees the lobby in the space hierarchy and
  does not see the group room**; joins the lobby unaided; survives a full reconcile tick; and cannot
  join the group room. Then: a user removed from the Authentik group is kicked from the group room and
  still sits in the lobby. Then: a deactivated user is gone from both, with no onbot code involved.
* A `docs/troubleshooting.md` note on the room-ID leak described above, and on encryption being
  irreversible in both directions of this decision.
* **An ADR — `docs/adr/0012-lobby-rooms.md`.** (0011 was taken by Session R's room-version-12 ADR.)
  ADR-0002 makes Authentik the source of truth and the
  reconciler total over room membership. A lobby is the first room where that is deliberately false:
  its membership is bot-injected group members *plus* people who exist only in Matrix, and the
  reconciler adds without ever removing. Record that the carve-out is confined to lobby rooms, that
  it needs no new state because "never kick" is a weaker invariant than "kick only whom we added",
  and that account deactivation — not the reconciler — is what removes a departing human from a lobby.

### Decisions already made

* A second room, not a visitable first room. History is the reason; it is not recoverable once shown.
* The group room is untouched: same join rule, same history, same encryption, same kicks.
* Lobbies are add-only. No ledger, no `to_kick` intersection, no exception carved into the group room's
  membership logic.
* Lobbies default to unencrypted and to injecting the group's members. Both are reversible before the
  room exists and one of them is not reversible after.
* Suffix, alias suffix and topic are config, defaulting to `" (Lobby)"` / `"-lobby"`. `& Friends` was
  rejected: the room needs a name that says *door*, not a name that says *more people*.
* Config fields are prefixed `visitor_lobby_`, not `lobby_`. The prefix carries the intent into a flat
  config file; the bare word does not.
* No room version is pinned. The floor is 10 (for `knock_restricted`); the version is otherwise the
  server's default. Pinning a number freezes the bot behind the ecosystem, and v12 is now that default.

---

## Deferred

* **Debug room** — a room where admins watch onbot's log messages. Shelved deliberately, not
  forgotten. The mechanics are easy (a `logging.Handler` feeding a bounded queue, drained by a task
  that batches records into `m.notice` messages); the hazards are that the handler must never do I/O
  inline, must not feed itself (posting logs through `httpx`, which logs, which posts), and that at
  `DEBUG` the bot logs every API call it makes — including bearer tokens — into a plaintext,
  unencrypted room that admins read on their phones and that cannot be un-synced once leaked. If this
  comes back: default the room's level to `WARNING`, and reuse `onbot/rooms/admin.py` from Session B.
