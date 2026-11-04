# 03 Reconciler

The pure diff modules are small and mostly right, but the engine around them has no safety rails for
destructive actions and no error isolation. An empty Authentik answer kicks everybody, and so does
switching off user sync. Any single room error aborts the whole pass, and that state is permanent
(REC-01 to REC-03). Rooms are identified by their canonical alias alone, so ordinary user actions can
break pairing (REC-04). Power-level handling is wrong for legacy pre-v12 rooms and does not withdraw
levels from users who left (REC-05, REC-08). Twelve findings are confirmed by throwaway experiments in
`/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/rec/test_rec_experiments.py`
(run with `PYTHONPATH=. uv run pytest <file> --rootdir=. -c pyproject.toml`; every test there asserts
the buggy behaviour and passes).

| ID | Severity | Title |
|---|---|---|
| REC-01 | high | No guard against mass removal when the Authentik user set collapses |
| REC-02 | high | `sync_authentik_users_with_matrix_rooms.enabled: false` kicks every member of every group room |
| REC-03 | high | One failing room aborts the whole pass, permanently |
| REC-04 | high | Rooms are paired by canonical alias only; the onbot state event is never consulted |
| REC-05 | high | In pre-v12 (legacy) rooms the bot grants PL 100 it can never withdraw |
| REC-06 | medium | A transient global-admin lookup failure kicks and demotes the admin everywhere |
| REC-07 | medium | `matrix_user_ignore_list` users are kicked, contrary to the documented promise |
| REC-08 | medium | Elevated power levels are not withdrawn from users who leave the group |
| REC-09 | medium | Space link and onbot state event are written only at creation and never repaired |
| REC-10 | medium | Switching a lobby off abandons it open; a manual close is reverted |
| REC-11 | low | The bot unblocks rooms that a human server admin blocked |
| REC-12 | low | The bot's own MXID can be written into `m.room.power_levels.users` |
| REC-13 | low | Alias collisions between groups are not detected; two groups fight over one room |
| REC-14 | low | Lifecycle orphan detection ignores the path and group filters of the sync scope |
| REC-15 | low | Discovery fingerprint misses inputs the reconciler acts on |
| REC-16 | low | Per-pass cost at scale: serial N+1 reads, duplicate scans, repeated teardown |
| REC-17 | info | Engine structure: three loops, implicit hand-off, duplicated group/lobby paths |

Checked and found fine: all list endpoints are fully paginated (Authentik `pagination.next`, Synapse
`next_token` / `next_batch`; the room members API is not paginated upstream). The run loop survives a
failing pass, and a trigger raised during a pass is kept for the next wait. Passes cannot overlap
inside one process. The admin `!` commands only read `last_reconcile_at`. v12 creator handling is
correct for rooms the bot created itself. `issues.md` #1 (lobby name/topic never re-synced) is
**fixed** in code (`engine.py:476`, commit `d332ad4`) but still listed as open there. `issues.md` #2
(per-group `keep_updating_matrix_attributes_from_authentik` ignored) is **still open**
(`engine.py:606`) and not re-reported here.

---

## High

### REC-01 No guard against mass removal when the Authentik user set collapses

- **Severity:** high
- **Category:** robustness
- **Location:** onbot/reconciler/engine.py:156-196, 448-468, 346-425; onbot/reconciler/membership.py:274-292; onbot/models.py:304-306
- **Status:** confirmed (experiment `test_empty_user_list_kicks_everyone`); the Authentik RBAC trigger needs live stack
- **Effort:** M

`desired_room_members` is computed purely from whatever `_gather_mapped_users` returns. Kicks are on
by default (`kick_matrix_room_members_not_in_mapped_authentik_group_anymore = True`). Nothing compares
the size of this pass's desired set with the last pass or with the current room population. Any of
these collapses the mapped set to (nearly) empty, and the next pass kicks every member of every group
room except the bot and global admins:

- the Authentik API token loses view permission on users. Authentik's list endpoints filter by object
  permission and answer `200` with an empty `results` list rather than `403`;
- `authentik_username_mapping_attribute` is changed to a path most users lack (every `compute_mxid`
  raises `KeyError`, each user is skipped with a warning), or the MAS localpart template changes (for
  example adds `| lower`, which `compute_mxid` cannot express);
- an Authentik version or query change stops embedding `groups_obj` in `/core/users/`.
  `MappedUser.group_pks` reads it with `.get("groups_obj", [])`, so every user silently belongs to no
  group;
- a user filter (`sync_only_users_*`) is edited to something that matches nobody.

The group side has the same shape: an empty `list_groups` answer makes every managed room "obsolete".
With `disable_rooms_when_mapped_authentik_group_disappears` on, that blocks every room and kicks every
member. With `delete_disabled_rooms` also on, it **purges every group room irreversibly** in one pass.
Recovery from the kick case is automatic once Authentik answers correctly (force-join), but members
lose the room in between, get a kick notification in every room, and in encrypted rooms lose the
keys for messages sent while they were out.

Fix sketch: a circuit breaker in the engine. Remember the previous pass's mapped-user count and
managed-group count. Refuse kicks, blocks and deletes for the pass (log loudly, emit a drift signal
to the admin room) when the new count is zero while rooms have members, or drops by more than a
configurable fraction (`max_removal_ratio`, default for example 0.2) or more than an absolute
`max_kicks_per_pass`. Additions stay allowed. An operator override (`!reconcile --force` or a config
flag) accepts the change.

### REC-02 `sync_authentik_users_with_matrix_rooms.enabled: false` kicks every member of every group room

- **Severity:** high
- **Category:** bug
- **Location:** onbot/reconciler/engine.py:157-159, 436-468
- **Status:** confirmed (experiment `test_sync_disabled_kicks_everyone`)
- **Effort:** S

The field is documented as the "Master switch for projecting Authentik group membership into Matrix
rooms". An operator turning it off expects the bot to stop managing membership. Instead
`_gather_mapped_users` returns `[]` and the membership pass still runs with `kick_enabled` taken from
the separate kick flag (default `True`). So the very next pass kicks every user from every group room;
only the bot and global admins stay. Group-room power levels are also recomputed over an empty member
list (harmless only because `managed` is then empty too).

Fix sketch: when user sync is disabled, skip `_converge_room_membership_and_levels` membership and
power-level work entirely (keep room attributes and lobbies' join rules if wanted), or at minimum force
`kick_enabled=False`. Add a test.

### REC-03 One failing room aborts the whole pass, permanently

- **Severity:** high
- **Category:** robustness
- **Location:** onbot/reconciler/engine.py:133-154, 271-311, 433-476, 406-425
- **Status:** confirmed (experiments `test_one_room_failure_aborts_pass`, `test_power_level_403_aborts_pass_before_later_rooms`); the exact Synapse status codes per trigger need live stack
- **Effort:** M

Every per-room step (`create_group_room`, `room_is_blocked`, `get_room_state`, `add_user_to_room`,
`kick_user`, `set_room_power_levels`, `set_room_name`, `set_room_topic`, `_disable_room`) runs
unguarded inside the loops over `group_maps`. Only avatar upload and lobby join-rule writes catch
errors. The first `ApiError` propagates out of `reconcile_once`. `run()` logs it and retries next
tick, which hits the same room in the same order and fails again. Everything after that room never
runs: later rooms' membership and power levels, space membership, lifecycle, `last_reconcile_at`, and
the `reconcile_completed` signal (so admin-room invites also stop converging). The only symptom is one
repeated stack trace.

Realistic permanent triggers (each a normal user or admin action, none needs a bug elsewhere):

- a room moderator **bans** a group member: the admin join API acts as that user and is refused;
- a moderator changes or clears the room's main address, so the next pass tries `createRoom` with an
  alias that is still taken (`M_ROOM_IN_USE`, see REC-04);
- a pre-v12 room where the bot must change a user at PL 100 (REC-05), or a v12 room where the users
  map would name a creator (REC-12);
- a human with PL 100 kicks or bans the bot from a legacy (pre-v12) group room, after which every
  state read there is `403`;
- in `_disable_room`, kicking a PL-100 member of a legacy room.

Fix sketch: converge each managed room in its own `try/except Exception` that logs the room id, group
pk and step, counts failures, and continues. Keep space membership, lifecycle and the completion
signal outside the per-room loop so they always run. Report the failure count in the completion log
line and `!status`. For add-user failures with `403`/`M_FORBIDDEN`, skip that user and keep going.

### REC-04 Rooms are paired by canonical alias only; the onbot state event is never consulted

- **Severity:** high
- **Category:** bug
- **Location:** onbot/reconciler/rooms.py:206-235; onbot/reconciler/engine.py:271-290, 210-232
- **Status:** confirmed by reading and by the abort mechanics in `test_one_room_failure_aborts_pass`; the Synapse responses (canonical alias semantics, `M_ROOM_IN_USE`) need live stack
- **Effort:** M

`build_group_room_maps` pairs a group with an actual room via `rooms_by_alias`, keyed on the
`canonical_alias` that the admin List Room API reports (the room's `m.room.canonical_alias` state).
The `group_room` / `visitor_lobby` state event carrying `group_id` is only read by the teardown path.
Consequences:

1. **A moderator can break the bot server-wide.** `m.room.canonical_alias` needs PL 50 under
   `private_chat` defaults, and the documented "Moderators" group pattern hands out PL 50. Changing the
   room's main address in Element, or clearing it, leaves the bot's alias in the directory but removes
   it from the listing. The next pass finds "no room" for the group and calls `createRoom` with
   `room_alias_name` set to a taken alias. That returns `400 M_ROOM_IN_USE` and, via REC-03, aborts
   every pass from then on. If the moderator instead deletes the directory alias, the bot silently
   builds a second, empty room and strands the first (point 2).
2. **Stranded rooms keep their members.** Changing `alias_prefix`, `matrix_alias_from_authentik_attribute`
   or the alias attribute creates a fresh room (documented). The old room still carries a live
   `group_id`, so teardown skips it. Since it is no longer in `group_maps`, nobody is ever kicked from
   it again, and power levels are never withdrawn there. People later removed from the group keep the
   old room and its history indefinitely.
3. **Any room wearing the alias is adopted.** That includes rooms the bot is not in and rooms created
   by a user who registered the alias first (Synapse's default `alias_creation_rules` let any local
   user create any alias). For a public squat room, the admin join API force-joins the whole group
   into a room the attacker controls as creator. For a room the bot is not in, the first state read is
   `403` and the pass aborts (REC-03). With the default `pk` alias this is hard to aim at a future group.
   It is easy with attribute or name-derived aliases.
4. ADR-0012 calls the `visitor_lobby` state type "the guard" against a lobby being treated as a group
   room. The pairing path never reads it. Only the alias naming scheme (dash in the default `-lobby`
   suffix, dashes stripped from group aliases) prevents the collision. A dashless
   `visitor_lobby_alias_suffix` plus attribute aliases can pair a lobby as some group's room, and its
   visitors are then kicked.

The space is resolved the same way (`_resolve_space`, engine.py:215-217): a changed space main address
makes the bot `createRoom` a space with a taken alias on every pass.

Fix sketch: make the state event authoritative. Per pass, identify managed rooms by their
`group_room`/`visitor_lobby` state (this read is already done for teardown; see REC-16 for making it
cheap) and pair by `group_id`. Fall back to the alias only to adopt a room that carries no state event
yet (legacy migration), and only if the bot is joined and has power there; stamp it on adoption.
Before `createRoom`, resolve the alias via the directory (`ApiClientMatrix.resolve_room_alias`
exists). If it already points at a room, adopt it when it is ours and otherwise log an actionable
error and skip this group, never abort the pass.

### REC-05 In pre-v12 (legacy) rooms the bot grants PL 100 it can never withdraw

- **Severity:** high
- **Category:** bug
- **Location:** onbot/reconciler/power_levels.py:24, 53-97; onbot/reconciler/engine.py:534-591
- **Status:** needs live stack (auth-rule behaviour is per spec; confirm on a v10/v11 room)
- **Effort:** M

ADR-0011's safety argument ("the creator is infinite, nobody can match it") only holds in rooms the
bot created as v12. This project replaces a legacy bot and adopts its group rooms by alias, and those
rooms are older versions where the bot is an ordinary `users` entry at 100. The engine still writes
`ROOM_ADMIN_LEVEL = 100` for Authentik superusers (`make_authentik_superusers_matrix_room_admin`,
default on), for every `global_room_admins` entry, and for any group whose
`chat-systemwide-powerlevel` is 100. That is the bot's own level. Pre-v12 auth rules forbid changing
the level of a user whose current level is greater than or equal to the sender's. Once granted, the
bot can never lower it:

- a superuser who loses superuser status, a name removed from `global_room_admins` (the feature's
  whole withdrawal mechanism, `_OnbotRoomState.global_admins`), or a member leaving a PL-100 group
  all produce a `set_room_power_levels` that the server rejects;
- with REC-03 that rejection aborts the pass every tick from then on;
- those users can also not be kicked by the bot (kick needs a strictly higher level), so
  `_disable_room` and membership kicks fail the same way.

`legacy_user_matches_or_outranks_creator` exists for exactly this question but is only used by the DM
migration, not by the power-level writer.

Fix sketch: read the bot's effective level per room (creator of a v12 room means infinite, else
`users[bot]` or `users_default`). Cap every granted level at `bot_level - 1` in rooms where the bot is
not an infinite creator, and log once per room. Skip (do not write) changes to users already at or
above the bot's level, with a warning naming the room, instead of failing the write.

---

## Medium

### REC-06 A transient global-admin lookup failure kicks and demotes the admin everywhere

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/reconciler/global_admins.py:181-184, 260, 265-272
- **Status:** confirmed (experiment `test_global_admin_lookup_failure_kicks_and_demotes_admin`)
- **Effort:** S

The module docstring promises that "a lookup failure against Authentik ... leaves the previously
resolved set in place ... rather than collapsing". The code does the opposite. `_lookup` swallows the
exception and returns `None`, `resolve` drops the name, and `self._mxids = frozenset(resolved)` shrinks
the set. This only affects admins found via the per-name lookup, which is the common case for a
global admin outside the synced paths or groups. On one Authentik `503` burst the same pass:

- removes the admin from `protected_ids` and `desired`, so they are **kicked from every group room**
  they are not a group member of;
- withdraws their PL 100 everywhere and rewrites the `global_admins` record in every room's state;
- on the next healthy pass force-joins them back and re-grants PL 100.

That means two state writes plus a kick and a join per room, notification noise for everyone, and in
legacy rooms REC-05 makes the withdrawal fail instead.

Fix sketch: have `_lookup` raise a distinct "transient" result. On any transient failure, keep the
previous resolution for that name (or the whole previous set), as `AdminResolver` does. Test both
paths.

### REC-07 `matrix_user_ignore_list` users are kicked, contrary to the documented promise

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/reconciler/engine.py:187-191, 455-460; onbot/config.py:1297-1310
- **Status:** confirmed (experiment `test_matrix_user_ignore_list_is_kicked`)
- **Effort:** S

The config and `docs/features.md` say ignored Matrix users are never kicked ("shields server admins,
other bots and bridge users from a sync that would otherwise see them as unknown to Authentik and
remove them"). The engine only uses the list to skip them from the mapped set. `protected_ids` for the
group-room diff is `[bot_id, *global_admins]`, so an ignored account that is in a group room (a server
admin who joined to help, a bridge bot) is kicked on the next pass. The same holds in `_disable_room`
(arguably fine there, since the room is being shut).

Fix sketch: add `*self.config.matrix_user_ignore_list` to `protected_ids` in the group-room diff. Add a
test that an ignored member in a group room survives with kicks enabled.

### REC-08 Elevated power levels are not withdrawn from users who leave the group

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/reconciler/engine.py:542-554; onbot/reconciler/power_levels.py:100-120
- **Status:** confirmed (experiment `test_power_level_not_withdrawn_when_member_leaves_group`)
- **Effort:** M

`managed` for a group room is `{u.mxid for u in members_in_room}`, the users who are in the group
*now*. The power_levels module docstring claims withdrawal (G8.4) "converging both up and down". That
only works for a user who stays in the group but loses the power-level group. A moderator removed from
the group, disabled in Authentik, moved out of `sync_only_users_*` scope, or put on
`authentik_user_ignore_list` is no longer managed, so their PL 50/100 entry stays forever. With kicks
disabled they remain in the room as a moderator. With kicks enabled the stale entry sits there until
they rejoin. Global admins solved the same problem with the recorded set in the room state event;
ordinary members did not get it.

Fix sketch: record the MXIDs the bot granted a level to in the room's onbot state (generalise
`global_admins` to `granted_levels`) and add them to `managed`. Alternatively treat every non-bot
entry in `users` whose MXID maps to a known Authentik user as managed. The first is safer because it
never touches levels a human set.

### REC-09 Space link and onbot state event are written only at creation and never repaired

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/reconciler/engine.py:274-290, 334-344; onbot/clients/matrix.py:215-229; onbot/clients/base.py:117-126
- **Status:** confirmed (by reading; experiment `test_room_without_state_event_is_never_stamped` shows an adopted room is never stamped)
- **Effort:** M

Room creation is three separate requests: `createRoom`, then `m.space.child` on the space, then the
onbot state event. Only the first is ever retried by convergence, because from the second pass on the
room is "found" by alias:

- if the process dies or a request fails between the steps, the room is never linked into the space
  and never gets its `group_room`/`visitor_lobby` state event. Without the state event the teardown
  path cannot recognise it, so it is never blocked when the group disappears. SIGTERM does not cancel
  a pass, so a long pass (REC-16) is regularly SIGKILLed mid-way by a 10 s container stop grace;
- `createRoom` is a `POST` that `BaseApiClient` retries on 5xx and transport errors. A gateway timeout
  on a successful create makes the retry fail with `M_ROOM_IN_USE` (aborting the pass), and leaves the
  room in exactly that half-created state;
- rooms that existed before the space was enabled, rooms adopted by alias from the legacy bot, and
  every room after the space was recreated (new room id) are never linked into the space. The lobby's
  join-rule pass does follow a new space id, so lobbies end up restricted to a space they are not in.

Fix sketch: converge `m.space.child` (with the right `suggested` flag) and the onbot state event for
every managed room on each pass, the same way avatars are converged: read, compare, write on
difference. Stamping also fixes adoption. Make `createRoom` non-retryable at the HTTP layer (or
resolve the alias before retrying).

### REC-10 Switching a lobby off abandons it open; a manual close is reverted

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/reconciler/rooms.py:224-226; onbot/reconciler/engine.py:299-311, 473-476, 489-492, 522-532; docs/adr/0012-lobby-rooms.md
- **Status:** confirmed (experiment `test_disabled_lobby_is_abandoned_open`)
- **Effort:** M

Setting `chatroom_visitor_lobby: false` (documented as "suppress a lobby"), or flipping
`visitor_lobby_enabled` off, makes `lobby_desired` `None`. The existing lobby then drops out of
management entirely. It stays `restricted` to the space, stays `suggested: true` in the space
listing, keeps its members, keeps any recorded global-admin PL 100, and teardown skips it because its
`group_id` is still live. There is no way to close a lobby short of deleting the group. Conversely,
ADR-0012 and the `_converge_lobby_membership_and_join_rules` docstring say "an operator can close a
lobby again by editing the room". In fact the join-rule pass rewrites any manual change back to
`restricted` on the next tick.

Fix sketch: decide the policy explicitly. Suggested: when a group no longer wants a lobby but a room
with its `visitor_lobby` state exists, set its join rule to `invite`, set the space child
`suggested: false` (or remove it), and withdraw recorded global admins. Optionally block it if
teardown is enabled. Fix the docstring and ADR wording either way.

---

## Low

### REC-11 The bot unblocks rooms that a human server admin blocked

- **Severity:** low
- **Category:** bug
- **Location:** onbot/reconciler/engine.py:287-290; onbot/clients/synapse_admin.py:264-267
- **Status:** confirmed (experiment `test_admin_block_is_undone`)
- **Effort:** S

The "group reappeared" unblock fires for any blocked managed room, regardless of who blocked it. A
server admin who blocks a group room for moderation or legal reasons sees the bot silently revert it
within one tick. Synapse's block status response includes the `user_id` that set the block.

Fix sketch: unblock only when the block was set by the bot (`user_id == bot_user_id`). Otherwise log
once and leave it, and maybe skip membership convergence for that room.

### REC-12 The bot's own MXID can be written into `m.room.power_levels.users`

- **Severity:** low
- **Category:** bug
- **Location:** onbot/reconciler/engine.py:187-195, 542-554
- **Status:** confirmed (experiment `test_bot_written_into_power_levels`)
- **Effort:** S

`_gather_mapped_users` does not exclude `bot_user_id`. If the bot's account is also an Authentik user
that is a member of a synced group and is a superuser or in a power-level group, its MXID lands in
`desired`/`managed` and is written into `users`. ADR-0011 forbids exactly this: v12 rejects naming a
creator there. The write fails, and with REC-03 every pass aborts. `GlobalRoomAdminResolver` already
excludes the bot; the main user path does not.

Fix sketch: skip `bot_user_id` in `_gather_mapped_users` (and defensively drop it in
`merge_power_levels`).

### REC-13 Alias collisions between groups are not detected; two groups fight over one room

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/reconciler/rooms.py:71-78, 206-235
- **Status:** confirmed (experiment `test_alias_collision_after_dash_stripping`)
- **Effort:** S

With an attribute-derived alias, two groups can produce the same alias, either by the same value or
after dash stripping (`team-a` and `teama`). Both map to the same room. Each pass then adds group A's
members and kicks group B's, then adds B's and kicks A's, forever. `alias_prefix` examples in the
config (`grp-`) also silently lose their dash.

Fix sketch: in `build_group_room_maps`, detect duplicate desired aliases (group and lobby aliases
together), log an error naming both group pks, and skip all colliding groups.

### REC-14 Lifecycle orphan detection ignores the path and group filters of the sync scope

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/reconciler/engine.py:629-663
- **Status:** plausible (cross-reference for 04-lifecycle)
- **Effort:** S

`_gather_orphaned_mxids` lists disabled users with only the attribute filter. It ignores
`sync_only_users_in_authentik_pathes` and `sync_only_users_of_groups_with_id`. A disabled Authentik
account outside the synced scope (a service account under `service/`) whose username equals the
localpart of a Matrix-only account that is not on `matrix_user_ignore_list` (a bridge or bot account)
is fed to the lifecycle and, once `dry_run` is off, logged out and later erased. The docstring's
"scoped to disabled Authentik users we can positively map" overstates the scoping.

Fix sketch: apply the same path and group filters as `_gather_mapped_users`, ideally through one
shared "sync scope" query helper used by the engine, discovery and the lifecycle feed.

### REC-15 Discovery fingerprint misses inputs the reconciler acts on

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/discovery.py:40-76, 153-157; onbot/reconciler/engine.py:440-445
- **Status:** confirmed by reading
- **Effort:** S

The poll fingerprints only groups passing `only_groups_with_attributes`. The engine also reads
power-level groups across *all* groups. The documented pattern is a separate "Moderators" group,
which usually does not carry the chat opt-in attribute, so changing its level from 50 to 100 is not
detected. Group `parent` (used by `only_for_children_of_groups_with_uid`) is not in `_group_facts`, and
global admins outside the synced set are not watched. Such changes wait for `server_tick_rate_sec`
(300 s) instead of `authentik_poll_rate_sec` (15 s).

Fix sketch: build the fingerprint from the same inputs the engine uses: include power-level groups
(their attribute value and `users`), `parent`, and the global admin lookups.

### REC-16 Per-pass cost at scale: serial N+1 reads, duplicate scans, repeated teardown

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/reconciler/engine.py:140-142, 287, 292-311, 372-404, 448-476, 575-583, 613; onbot/clients/synapse_admin.py:210-214; onbot/discovery.py:135-157
- **Status:** plausible (counted by reading; needs live stack for timings)
- **Effort:** M

Everything runs strictly sequentially. At 2000 users, 300 group rooms and 100 lobbies a steady-state
pass costs roughly:

- two full server-wide room listings (`list_spaces` and `list_non_space_rooms` each call `list_rooms`);
- per group room: block status, onbot state (read twice: avatar and power levels), members, power
  levels and topic, so about 6 GETs. Per lobby: about 6 more;
- with teardown enabled, 2 state GETs for *every* unmapped room on the server (each user's notice-board
  DM, for example, so about 4000 GETs);
- every room that was already disabled is re-blocked and re-cleared on every pass, because the bot
  stays in it and its state still names a dead group;
- one `user_synced` emit per user, awaited serially.

That is several thousand sequential requests per pass, which also widens the SIGKILL window from
REC-09. Separately, discovery pulls every user (with `groups_obj`) and every group (with embedded
`users_obj`) from Authentik every 15 s.

Fix sketch: list rooms once and split. Read each room's onbot state once per pass and pass it along.
Remember disabled rooms (state flag) and skip them. Skip the obsolete scan for rooms known to be other
onbot types, or keep a managed-room index. Run per-room convergence with a small bounded concurrency
(`asyncio.Semaphore`, combined with REC-03's per-room isolation). Pass `include_users=false` to group
listings that need only pks, if the deployed Authentik supports it.

---

## Info

### REC-17 Engine structure: three loops, implicit hand-off, duplicated group/lobby paths

- **Severity:** info
- **Category:** simplification
- **Location:** onbot/reconciler/engine.py:133-154, 271-344, 433-591
- **Status:** confirmed by reading
- **Effort:** L

A room's convergence is spread over three loops over `group_maps` (`_converge_rooms`,
`_converge_obsolete_rooms`, `_converge_room_membership_and_levels`). They communicate by mutating
`gm.room` / `gm.lobby` in the first loop. Group room and lobby have parallel but different code for
creation, stamping, avatar, membership, power levels, join rules and attributes. The fresh
`GroupRoomState(...)` / `VisitorLobbyRoomState(...)` is rebuilt in six places. The real differences
between the two kinds are a handful of policy values: room type, kick or add-only, which users get
group power levels, whether a join rule is governed, and `suggested`.

Fix sketch: a small `ManagedRoom` record (room, `OnbotRoomType`, desired attributes, membership
policy, power-level policy, join rule, `suggested`), built once per pass from `GroupRoomMap`. Then one
`_converge_managed_room(mr)` that runs every step (ensure exists, stamp, link to space, avatar,
attributes, membership, power levels, join rule) inside the per-room `try` from REC-03. This also
fixes REC-09 by construction, makes `issues.md` #2 a one-line change, and shrinks `engine.py`
noticeably. The pairing in REC-04 becomes a single function that returns `ManagedRoom`s.
