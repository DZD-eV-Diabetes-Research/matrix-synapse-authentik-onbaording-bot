# Troubleshooting

| Symptom | Likely cause |
|---|---|
| Users never get added to rooms | The MXID localpart contract is broken. `authentik_username_mapping_attribute` does not match MAS's localpart template, so computed MXIDs do not exist. See [configuration.md](configuration.md#the-mxid-localpart-contract). |
| `User identity: ... now map to another Matrix ID` in the admin room | A user was renamed in Authentik, or `authentik_username_mapping_attribute` changed. Onbot keeps the account the user already owns; nothing to do unless the old account is wrong. See [configuration.md](configuration.md#renamed-users-keep-their-account). |
| `User identity: ... resolve to the Matrix account` in the admin room | Two Authentik users claim one account (a duplicate `matrix_name`, or a new user named like a renamed one). All of them are skipped until you fix the attribute in Authentik, or edit the binding in the bot's `known_users` account data ([how](configuration.md#resolving-a-conflict-or-a-wrong-binding-by-hand)). |
| `User identity: ... names the Matrix account ... but MAS links that account to` | A user never seen before names an account MAS links to somebody else (also when that owner is disabled, outside the sync scope or never seen by Onbot). The owner is managed as usual; the other user is skipped until their mapping attribute is fixed in Authentik. |
| `MAS has N upstream link(s), but none matches` | `mas_admin.upstream_subject_attribute` does not match the subject mode of the Authentik provider MAS uses (`uid` for the default hashed ID, `pk` for the user ID). Until fixed, users resolve from the recorded bindings and the mapping attribute, but a user Onbot has not seen before whose attribute names a MAS-linked account is skipped (reported as "MAS links that account to another Authentik user"). |
| `could not read the upstream links from MAS` | MAS is down or refuses the admin client. Until it answers, users resolve from the recorded bindings; users Onbot has not seen before are skipped and their accounts left alone (no join, kick, lock or erase), so an unchecked guess never becomes a binding. |
| A group did not become a room | It failed a filter, or is on `authentik_group_id_ignore_list` (which wins over everything). Walk the checklist in [authentik-groups.md](authentik-groups.md#checking-your-work). |
| A group attribute (topic, lobby, power level) is ignored | Most often the `attributes.` prefix: topic, lobby and create-params config values need it, `authentik_group_attr_for_matrix_power_level` and `room_avatar_url_attribute` must omit it. See [authentik-groups.md](authentik-groups.md#the-path-gotcha-when-to-write-attributes). |
| `reconcile: room not converged:` in the log | One room could not be fully converged this pass; the rest of the pass, offboarding included, ran normally. The line names the room, the Authentik group and each problem: a member the server refuses to join (banned), a member Onbot may not remove or demote (a room creator, or somebody at the bot's own level 100 in a room older than version 12), or a refused write. Fix it in the room; Onbot retries every pass. |
| A moderator kept their level after leaving the group | The level was set by hand, or changed by hand after Onbot set it: Onbot only withdraws levels it set itself and that nobody changed since. Levels granted before Onbot started recording its grants are adopted on the first pass only if the user still qualifies. |
| A moderator stays at power level 0 | The group's power-level attribute is not a real integer (a quoted `"50"` or a boolean is skipped silently), or the user is not a member of that room. |
| `invalid room-create-params JSON` in the log | `chatroom_params` was written as a YAML mapping. It must be a *string* containing JSON. That attribute is a niche escape hatch most deployments should not set at all, so also ask whether the group needs it. See [authentik-groups.md](authentik-groups.md#the-yamljson-trap). |
| Disabled users keep their Matrix access | `mas_admin` is not configured on a MAS-backed homeserver: Onbot then refuses to lock anybody, because the Synapse admin API cannot end a MAS session (the log shows `offboarding: the homeserver delegates logins to ...` and `REFUSED logout of ...`, the admin room gets one report). Configure `mas_admin`. Or the lifecycle `dry_run` is still `true` (the default). Right after a lock, a client already logged in keeps working for about two minutes (Synapse's token cache). |
| A re-enabled user's old sessions work again | The configured MAS cannot end sessions through its admin API (0.14, for example), so a lock only suspends them. The startup line `MAS admin API capabilities:` says so. Upgrade MAS. |
| `FAILED erase of @user ... refusing to deactivate` | The configured MAS always GDPR-erases on deactivation (no `skip_erase` in its admin API) but `gdpr_erase_on_delete` is `false`. Upgrade MAS, or set `gdpr_erase_on_delete: true` if a GDPR erasure is what you want. |
| `QUARANTINED the lifecycle entry of @user ...` in the audit log | That entry of the bot's lifecycle account data does not validate (a hand edit, for example). It is kept verbatim under `quarantined` and the account is left alone; repair or remove it there. |
| `ALERT the lifecycle ledger is missing ...` in the audit log | The bot's lifecycle account data was lost (bot account recreated, `server_name` changed) while MAS has locked users. Onbot no longer knows which locks it set; unlock re-enabled users in MAS by hand. On a first start the locks were set by somebody else and the line can be ignored. |
| Nothing destructive ever happens | Expected by default. The lifecycle is quarantined (`dry_run: true`) and only logs to the `onbot.lifecycle.audit` channel until you opt in. |
| A user deleted in Authentik keeps their Matrix access | Onbot only offboards deleted users it has seen before: active, inside the sync scope and with a Matrix account. Someone deleted before Onbot ever saw them that way is unknown to it. Lock or deactivate the account in MAS by hand. |
| `FROZEN @user: ...` in the audit log | The user was being offboarded but left the sync scope (while active, or without ever being known to Onbot as active), landed on an ignore list, or another active Authentik user now maps to the same Matrix ID. Onbot holds the account as it is (still locked if it was locked) until you decide; see [features.md](features.md#offboarding). |
| `FAILED logout of @user ... LifecycleTargetNotFound` | MAS has no user with that localpart: `authentik_username_mapping_attribute` does not match MAS's localpart template. Onbot retries with a growing delay and does not advance the user. |
| `FAILED ...` with an HTTP 404 from `/api/admin/v1/users/by-username/` | The MAS admin API is not reachable at `mas_admin.url` (the listener does not mount the `adminapi` resource, or a proxy does not route `/api/admin`). `onbot healthcheck --deep` fails for the same reason. |
| `guard: paused N destructive action(s)` in the log, or a "Destructive actions paused" report in the admin room | Authentik answered with far fewer users or groups than before (or none), or one pass would remove more than `max_reversible_actions_per_pass`, or erase accounts or delete rooms (irreversible) at least `confirm_irreversible_actions_from` times. Nothing was removed. Check the API token's permissions and your filters; if the change is intended, `!confirm <id>` in the admin control room releases the batch, `!reject <id>` holds it while the situation lasts. See [features.md](features.md#guard-against-mass-removals). |
| `guard: the guard state ...` at error level, or "The destructive-action guard cannot read its state" in the admin room | The guard's account data does not validate (edited by hand, or written by a newer Onbot). Every removal is paused until an admin runs `!guard reset` in the admin control room, which keeps a verbatim copy. See [features.md](features.md#guard-against-mass-removals). |
| Members who left a group are not kicked, or disabled users not locked, and nothing is reported | The guard paused them and the admin control room is switched off (`admin_room.enabled: false`), so nobody can confirm. Enable `admin_room` with at least one admin, or raise the `destructive_action_guard` limits for an intended large change. |
| `lifecycle step aborted: Authentik returned no users at all` | The Authentik API token can no longer list users (Authentik answers an empty list, not an error, when a token lacks view permission). Offboarding is skipped until the token sees users again. |
| Welcome DM send fails with a 500 | The bot device is not registered yet (MAS-backed servers only). Onbot registers it in the background at startup and retries until Synapse answers; welcomes start once it succeeds. Look for `startup step 'device registration'` in the log. |
| `startup step '...' still failing` in the log | Synapse or MAS was not reachable yet when onbot started. The step is retried with a growing delay (up to 60 s) and heals by itself once the server is up; `onbot healthcheck` lists steps still retrying. A step the server *refused* (a 4xx such as 403) is logged once as an error and not retried; fix the cause and restart. |
| Container is `unhealthy` | `onbot healthcheck` prints which part is stale. `reconcile`: no pass has succeeded for 3 reconcile intervals plus 5 minutes (Authentik or Synapse down, or every pass failing; the log says why). `sync`: no sync response for 5 minutes. `no heartbeat file`: the service is not running, or cannot write `/tmp` (see [deployment.md](deployment.md#healthcheck)). |
| `onbot healthcheck --deep` reports a dependency FAIL | Read the per-dependency log line; it distinguishes unreachable from auth-rejected. The `matrix-cs` line also flags a token or `bot_user_id` mismatch. |
| `mas-admin FAIL: admin API authorized, but MAS has no user ...` | MAS answered, but does not know the bot's own username. `mas_admin.url` points at a different MAS than the one behind the homeserver, or `bot_user_id` is wrong. Every lock and deactivation would fail the same way. |
| `listing ... changed while it was read` in the log | Users, groups or rooms were added or removed in Authentik or Synapse while Onbot paged through them, twice in a row. The pass is failed rather than run on a list with gaps (a missing user would be kicked); it is retried on the next tick. Only listings longer than one page (100 entries) are affected. |
| `room of group X skipped: its alias #... belongs to room !..., which the bot did not create` | Somebody else created a room at the alias this group would get (with an attribute alias it is predictable), or the rooms were created by another bot account. Onbot never uses such a room. Remove the alias from that room (or delete the room) as a server admin; the group gets its room on the next pass. |
| `... skipped: its alias #... points at room !..., which is the room of group Y` (or `carries no onbot marker`) | The alias belongs to another of Onbot's rooms: two groups map to one alias, or a room of an older version lost its marker. Give the groups distinct aliases. A room without a marker is never adopted; move the alias away from it. |
| `rooms !a, !b all claim to be the room of group X` | Two rooms carry Onbot's protected marker for the same group. Onbot does not guess which one is right and skips the group. Delete or close the wrong room; then remove its marker as a server admin, or delete the room. |
| `... carries an onbot group_room marker written by @..., not by the bot` | Somebody other than Onbot rewrote the room's marker: a moderator in a room from an older version, or a user at Onbot's own level in a room older than version 12. Onbot does not trust, adopt or tear down that room; if it was a group's room, the group is skipped. Onbot cannot tell what the marker said before, so restoring it by hand does not help (the sender stays a human). Delete the room, or move the group's alias away from it so that Onbot creates a new room. (A change Onbot notices while it runs is repaired by Onbot itself, see the next line; only a change made while it was stopped ends here.) |
| `Rooms: room !..., the room of group X: the onbot marker was removed` (or `rewritten`, or `the power level required to change its onbot marker had been lowered`) in the admin room | Somebody at Onbot's own level changed the marker or its protection in a room Onbot already manages: an extra creator from `chatroom_params` in a version 12 room, or a user at level 100 in an older room. Onbot restored it from what it knows about the room and reports each change once. Find out who did it in the room's history; if it keeps happening, take that user's level away. |
| `Rooms: room !..., the room of group X, was blocked by @...` in the admin room | A server admin blocked the room. Onbot only lifts its own blocks, so it leaves the room blocked and stops managing it (no joins, kicks, levels or renames) until the block is lifted with Synapse's admin API (`PUT /_synapse/admin/v1/rooms/<room_id>/block` with `{"block": false}`). |
| `Rooms: the visitor lobby !... of group X was closed by @...` in the admin room | A room admin of the lobby made it invite-only (or knock, or private). It stays closed (D16). To hand it back to Onbot, run `!lobby reopen X` in the admin control room. |
| `Rooms: the alias #... of room !... belongs to room !...` in the admin room | The room's alias was removed and another room took it before Onbot could put it back. The room works without it. To give it back, remove the alias from the other room (a server admin can); Onbot recreates it on the next pass. |
| `room !... carries an onbot marker from an older version ... not adopted` | A room from before room identity that is not at the alias the configuration gives its group (an earlier alias change stranded it). Onbot leaves it alone. Delete it, or move the group's alias to it if it is the right room. |
| `room !... was upgraded to !... by somebody other than the bot` | A human upgraded the room. Onbot does not follow such upgrades and skips the group until an admin decides (for example by recreating the room). |
| `room identity: the state of N room(s) the bot created could not be read` | Synapse failed to answer for some of Onbot's rooms. Onbot does not create, adopt or tear down any room until it can see all of them; it retries every pass. |
| `the admin control room #... stays off` | The control room alias belongs to a room Onbot did not create, or to another of its rooms. Onbot never takes it over. Remove the alias from that room, or set another `admin_room.alias`. |
| `createRoom failed (...), but the server had created the room` | The answer to a room creation was lost (proxy timeout, dropped connection), but the room exists. Onbot found it by the marker it puts into every room it creates and carries on with it, so no second room is made. Nothing to do. |
| `... failed (...) and did not take effect; sending it once more` | A kick or a forced join got no clear answer; Onbot checked the membership, saw the change had not happened, and sent it again. A kick or join that did happen is not repeated. |
| HTTP requests are no longer logged | Since the HTTP layer review, `httpx` and `httpcore` log their per-request lines only at `log_level: DEBUG`. |
| `sync position rejected by the homeserver` in the log | The homeserver forgot the sync connection (expired, purged, or a database restore). Onbot starts a fresh connection by itself; recent events are replayed, which the admin room and the welcome flow both ignore. Nothing to do unless it repeats constantly. |
| `docker stop` takes 10 s and the exit code is 137 | The container was killed. Make sure the image's entrypoint (`tini`) is not overridden, and that `stop_grace_period` is not shorter than 8 s. See [deployment.md](deployment.md#stopping). |
| Sliding sync unavailable | The homeserver does not advertise MSC4186. Onbot falls back to the reconciler signal path automatically. |
| A user can still write in their welcome room | Their room predates onbot's read-only welcome room. See below. |
| A user is in their welcome room without ever accepting an invitation | Expected: `force_join_onboarding_room` is on by default. |
| Somebody who joined a group room got no welcome room | Expected unless a reconcile pass maps them: only active Authentik users in the sync scope with a Matrix account are welcomed (and receive broadcasts). Guests from other homeservers and local accounts outside Authentik never are. A new user is welcomed with the first pass that sees their Matrix account. |
| `!announce` answers `not sent: no reconcile pass has mapped the users yet` | Right after a start, before the first pass finished, or with `sync_authentik_users_with_matrix_rooms.enabled: false`. Wait for the pass, or turn user sync on. |
| `!announce` or `onbot broadcast` answers `not sent: no users are mapped` | Authentik returned nobody in the sync scope: Authentik unreachable, an API token that cannot list the users, or a scope filter (`sync_only_users_*`) that matches nobody. Fix that; nothing was sent. |
| An announcement takes minutes, and its result says the homeserver rate-limited the bot | The bot's send rate limit is not lifted (`!status` shows `send rate limit: NOT lifted`; the log has the reason of the failed `override_ratelimit` call, usually missing admin rights or a wrong `admin_api_path`). The broadcast still reaches everybody, at the homeserver's pace. Fix the override and restart. |
| `!announce` answers `An announcement is still being sent` | One announcement runs at a time. Wait for the result of the first one, then send the second again. |
| Editing an `!announce` does nothing | Intended: edits are not commands. Send a new message. |
| An admin who left the control room is not invited back | Intended: declined or left is remembered. Remove them from `admin_user_ids` or the admin group and add them again to send a new invitation. |
| A new logo at the same avatar URL does not show | It is picked up on the next start of the bot (each URL is downloaded once per start). |
| `admin control room ... is end-to-end encrypted` (or `new notice boards are end-to-end encrypted`) at error level | Synapse encrypts new private rooms by itself. See [Encrypted control room or notice boards](#encrypted-control-room-or-notice-boards) below. |
| `cannot welcome @user: the onbot state of their notice board ... cannot be read` | The bot's state event in that room was edited by hand into something it cannot parse. Onbot neither guesses nor retries until the next start. Correct or remove the state event named in the line. |
| `visitor_lobby_enabled requires create_matrix_rooms_in_a_matrix_space` at startup | A lobby's `restricted` join rule needs a space to be restricted to. Enable the parent space, or turn the lobby off. |
| A lobby stayed invite-only | A lobby enabled by an Authentik attribute while no parent space is configured is skipped, not created invite-only. Configure the space. |
| Someone outside the group can see the group room exists | Expected, and only its *existence* leaks — not its contents. See "The lobby room-ID leak" below. |

For the healthcheck details, see [deployment.md](deployment.md#healthcheck). For the auth topology
behind several of these, see [architecture.md](architecture.md).

## The lobby room-ID leak (and why a lobby is a *second* room, not a visitable first one)

A visitor lobby (ADR-0012) makes a group *reachable* — a space member outside the Authentik group
finds the lobby in the space listing and joins it — while the group's own room stays private. The
group room is left untouched precisely because opening it retroactively exposes its history:
`history_visibility` defaults to `shared`, so the first visitor to an unencrypted group room can
scroll back to the day it was created. A lobby starts empty, so it has no history to leak.

What a lobby does **not** hide is the group room's *existence*. The `m.space.child` event the bot
writes onto the space carries the private room's ID, and space state is readable by any space member;
likewise any user can resolve `#group:server` to a room ID through the room directory. A non-member
still **cannot** join the group room, read its history, see its name or topic, or enumerate its
members — Element will not show it to them — but a curious person with `curl` can learn that a room
with that ID exists.

If that is unacceptable, the answer is **not** this feature — it is a **second space**. Put the
private group rooms in one space and the lobbies in another, and do not add the outside audience to
the first. Do not try to paper over the ID leak within a single space.

## Encryption on a lobby (and the group room) is irreversible

Encryption cannot be turned off once a room has it, and it cannot be turned on retroactively without
recreating the room. This cuts both ways:

- A **lobby** is unencrypted by default (`visitor_lobby_end2end_encryption_enabled: false`), weaker
  than the group room's own default. A room open to the whole space gains little from encryption,
  which would instead greet every visitor with a screen of "unable to decrypt". Turn it on before the
  lobby is first created if you want it; you cannot turn it off afterwards.
- A **group room** you created encrypted stays encrypted forever. There is no supported flip.

Because both directions are one-way, the lobby's default is chosen so the decision is made *before*
the room exists rather than discovered after.

## Welcome rooms created before onbot made them read-only

The bot's 1:1 welcome room is a notice board: only the bot may post in it. Rooms onbot creates today
are shaped that way at creation, giving the bot power level 100 and the user 0. On every welcome the
bot re-reads the room's power levels and repairs them if somebody changed them by hand.

Rooms created by an earlier onbot were `trusted_private_chat` rooms, which grant the invited user
power level 100 — the same as the bot. The Matrix specification forbids changing a power-level entry
greater than or equal to your own, so **the bot cannot demote those users**, and it cannot raise
itself above them either, because nothing outranks 100. Synapse's `make_room_admin` admin API does
not help; it also tops out at 100.

Those rooms therefore stay writable by their user. The only way out is to delete the room
(`DELETE /_synapse/admin/v1/rooms/<room_id>` with `purge`). The bot notices on the next check of
that user (after a restart, or when they leave a room) that it is no longer in the room, drops the
entry from its `m.direct` account data and creates a new notice board. Be aware that this
also re-sends every welcome message, because the per-message bookkeeping lives in the state of the
room being destroyed — on a live server it pages every user it touches.

## Encrypted control room or notice boards

The bot has no end-to-end encryption ([ADR-0009](adr/0009-e2ee-stance.md)) and creates the admin
control room and the notice boards unencrypted. Synapse's `encryption_enabled_by_default_for_room_type`
(`invite` or `all`) encrypts every new private room anyway, and a client cannot opt out when it
creates a room (verified against Synapse 1.164: an `m.room.encryption` event in `initial_state` can
only choose the algorithm). Encryption can never be removed from a room again.

- **Control room:** admins' clients encrypt their commands, and the bot never sees them. Onbot logs
  an error at startup and posts a notice into the room saying so; commands sent in plain text and the
  bot's own reports still work. Fix: set `encryption_enabled_by_default_for_room_type: off` in
  Synapse, then delete the control room as a server admin
  (`DELETE /_synapse/admin/v1/rooms/<room_id>` with `purge`) and restart Onbot, which creates a new,
  unencrypted one.
- **Notice boards:** the welcome messages and announcements still arrive, but as plain text in an
  encrypted room, which clients flag. Onbot logs an error once per start when it creates such a
  board. Turning the Synapse setting off fixes boards created afterwards; existing ones stay
  encrypted unless you replace them (see the previous section for what that costs).
