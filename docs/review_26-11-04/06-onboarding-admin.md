# 06: Onboarding and admin features (welcome flow, notice board, control room, broadcast, avatar, media)

The notice board itself is sound: the power-level override really leaves the user at 0 against a 50/100 gate on every action that matters, the per-user lock does stop the "force-join event races the first welcome" duplicate, and broadcast retries reuse their transaction id. The weak spots are the bookkeeping writes *around* room creation:
`m.direct` is read-modify-written without a cross-user lock (confirmed lost update, which produces duplicate notice boards and a full re-welcome), and anything failing between `createRoom` and the first state write silently and permanently drops the force-join, the space link and the avatar (confirmed).
The broadcast path is fragile at scale: without a working rate-limit override most sends fail (confirmed by simulation), and the failure report itself can exceed the maximum event size, so the admin then gets no reply at all.
Cutover from the legacy bot and servers that encrypt private rooms by default are both unhandled and undocumented.

Experiments live in `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/onb/` (`exp_welcome.py`, `exp_control.py`, `exp_ratelimit.py`). They import the unit-test fakes and the real services; run them with `uv run --offline python <file>` from the repo root.

| ID | Severity | Title |
|---|---|---|
| ONB-01 | high | Concurrent welcomes of two different users lose an `m.direct` entry, leading to duplicate notice boards and a full re-welcome |
| ONB-02 | medium | A failure between `createRoom` and the first state write permanently skips force-join, space placement and avatar |
| ONB-03 | medium | Broadcast fails for most rooms whenever `override_ratelimit` did not take effect; 429 retries ignore `retry_after_ms` |
| ONB-04 | medium | The `!announce` result reply exceeds the 64 KiB event limit on a large partial failure, so the admin gets no answer |
| ONB-05 | medium | Cutover from the legacy bot re-welcomes every user into a second room; legacy DMs are invisible to `m.direct` lookup |
| ONB-06 | medium | Server-side default encryption (`encryption_enabled_by_default_for_room_type`) makes the control room deaf and is never detected |
| ONB-07 | low | A user who leaves the notice board can never get back, and broadcast/`!status` still count their room (and erased users' rooms) as reached |
| ONB-08 | low | A DM the bot can no longer read (bot not in room, unparsable state) fails the welcome on every tick forever, with no recovery |
| ONB-09 | low | Welcome delivery is at-least-once, not once: a crash or failed state write after a send re-sends that message |
| ONB-10 | low | Announcements are sent as plain `body` only: users see raw markdown, unlike the welcome messages |
| ONB-11 | low | An edit (`m.replace`) without the `* ` fallback prefix is executed as a new `!announce` |
| ONB-12 | low | An admin who rejects or leaves the control room is re-invited on every reconcile tick |
| ONB-13 | low | The sync join path welcomes anyone seen joining any room, bypasses Authentik scope filters, and on startup replays every member of every room |
| ONB-14 | low | An expired sliding-sync `pos` is retried forever, leaving the control room and sync onboarding dead until restart |
| ONB-15 | low | Avatars are keyed by URL only: a changed image at the same URL never propagates, and notice boards never follow a changed `bot_avatar_url` |
| ONB-16 | low | Room provisioning is implemented three times, and each writes its marker state after `createRoom` instead of in `initial_state` |
| ONB-17 | low | The control room alias is trusted without checking the `admin_room` marker |
| ONB-18 | info | Message identity is the raw configured text: typo fixes and YAML reformatting re-page every user |
| ONB-19 | info | `todo.md` is stale for Session A and its header; broadcast silently needs welcome messages configured |

## High

### ONB-01 Concurrent welcomes of two different users lose an `m.direct` entry

- **Severity:** high
- **Category:** bug
- **Location:** onbot/onboarding/welcome.py:131, onbot/onboarding/welcome.py:190-204, onbot/onboarding/listener.py:75-78, onbot/app.py:242
- **Status:** confirmed (experiment `exp_welcome.py`, `exp1`, using the real `WelcomeService` with a fake client that yields on each HTTP round trip)
- **Effort:** S

`_ensure_direct_room` reads the bot's whole `m.direct` blob, creates a room, appends one entry and writes the blob back. The only serialisation is `self._locks.setdefault(mxid, ...)`, a lock *per user*. Two welcomes for *different* users run concurrently whenever the reconciler signal path (inside `reconcile_once`) and the sync pump (`OnboardingListener.handle_sync`) are both creating DMs; they run as separate tasks under `asyncio.gather` in `run_service`. Both read the same old blob, and the second write drops the first user's entry.

When this happens in practice: the first sliding-sync response asks for `required_state: [["m.room.member", "*"]]` for up to 1001 rooms, so on startup the sync handler walks *every member of every room the bot is in*, while the reconciler's first pass walks every mapped user. Whenever many users still lack a DM (first deployment against an existing server, enabling `welcome_new_users_messages` on a running server, a bulk import, or the legacy cutover in ONB-05), both paths create DMs at the same time and entries are lost.

Experiment output: two users welcomed concurrently, two rooms created, `m.direct` afterwards holds only `@bob`. After a simulated restart the lost user gets a third room and the welcome message again:

```
EXP1 rooms created: 2 m.direct: {'@bob:matrix.test': ['!dm-2:matrix.test']}
EXP1 after restart rooms created: 3 messages sent: [('!dm-1', 'hi'), ('!dm-2', 'hi'), ('!dm-3', 'hi')]
```

Outcome for the user: two "Announcements" rooms, both force-joined, the full welcome sequence twice. The orphan room is unknown to broadcast, so it goes stale while looking identical to the live one. Within the same process nothing heals it (the listener's `_welcomed` cache skips the user); the duplicate appears on the next restart.

Fix sketch: guard the read-create-write section with one process-wide `asyncio.Lock` (the bot is a single process), and re-read `m.direct` inside that lock immediately before writing. Longer term, treat the room's own `direct_room` state event (which carries `user_id`) as the source of truth and `m.direct` as a cache that a startup scan can rebuild. That also gives orphan detection and legacy adoption (ONB-05). Add a unit test that welcomes two users under `gather` with a yielding fake.

## Medium

### ONB-02 A failure between `createRoom` and the first state write permanently skips force-join, space placement and avatar

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/onboarding/welcome.py:136-143, onbot/onboarding/welcome.py:197-206, onbot/onboarding/welcome.py:65 (`_FORCE_JOIN_SOFT_FAILURES`)
- **Status:** confirmed (experiment `exp_welcome.py`, `exp2` and `exp2b`)
- **Effort:** S

The force-join, the space link and the avatar happen only on the code path where `created` is true, which exists only inside the one call that created the room. `m.direct` is written at line 204, *before* `_maybe_place_in_space` (205), `_maybe_set_avatar` and `_force_join` (138), and the `DirectRoomState` is first persisted only after the force-join (140). If anything raises in that window, the next call finds the room in `m.direct`, takes the `created=False` branch, and loads an empty `DirectRoomState` (no `force_joined_at`). It then sends the welcome messages into a room the user was only *invited* to, and never retries the join, the space link or the avatar.

Triggers:

* `link_room_to_space` or `resolve_room_alias` failing with a 5xx after retries (`_maybe_place_in_space` is not wrapped in `try`, unlike the avatar);
* the force-join returning anything except 403/404, for example 500/502/503 after retries, or 400 for a user the admin join API refuses. `_force_join` re-raises those by design ("not swallowed"), but the retry it relies on never happens;
* the process being stopped (deploy, SIGTERM) inside that window.

Experiment: a 502 from the space link on the first call, then a healthy server:

```
EXP2 force-joins: [] ... 'force_joined_at': None} sent: [('!dm-1', 'hi')] space links: []
EXP2b force-joins after recovery: [] ... 'force_joined_at': None
```

The user ends up with an unaccepted invite holding the welcome messages, which is exactly what force-join exists to prevent ("there is no invitation to accept" in `docs/features.md`).

Fix sketch: stop keying these steps on `created`. Persist the `DirectRoomState` at creation time (ideally as `initial_state` of `createRoom`, see ONB-16). Then decide the force-join from state plus membership: `force_joined_at is None` and the user's membership is `invite` (never joined) means join now, while membership `leave` means the user left, so do not drag them back. Make the space link and avatar "ensure" steps on the existing-room path too, or record their completion in the state event.

### ONB-03 Broadcast fails for most rooms whenever `override_ratelimit` did not take effect

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/admin/broadcast.py:124-137, onbot/clients/base.py:114-123, onbot/app.py:64-80
- **Status:** confirmed (simulation `exp_ratelimit.py`: real `ApiClientMatrix` and real tenacity retry over an `httpx.MockTransport` token bucket with Synapse's default `rc_message`, 0.2/s and burst 10)
- **Effort:** M

The module docstring is honest that the shared retry is "4 attempts, ~3.5s, ignores `retry_after_ms`". The consequence is worse than "slower": with Synapse's default limiter a token arrives every 5 s, the retry budget per room is 3.5 s, and five workers burn through the queue failing. Simulation with only 30 rooms:

```
rooms=30 sent=12 failed=18 in 14.0s
```

At a few hundred users, almost everything after the initial burst fails. The override is best-effort and only logged at startup (`_relax_bot_ratelimit`). It can fail for ordinary reasons (the admin API is not reachable on the configured path, or a MAS token lacks the Synapse admin scope), and nothing tells the admin issuing `!announce` that the bot is still throttled. The integration stack sets `rc_message` to 1000/s (`tests/integration/stack/synapse/homeserver.yaml:60`), so no test ever sees this.

Fix sketch: honour `retry_after_ms` for 429 (a tenacity `wait` that reads it from `ApiError.payload`, capped), and give the broadcast its own retry budget, or a second pass over the failed rooms after a pause. Record whether the override succeeded and show it in `!status` and in the `!announce` reply. Add an integration test with a realistic `rc_message`.

### ONB-04 The `!announce` result reply exceeds the maximum event size on a large partial failure

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/admin/broadcast.py:76-81, onbot/admin/broadcast.py:131-133, onbot/admin/control_room.py:155-160 (`_announce`), onbot/clients/base.py:43 (`ApiError.__str__`)
- **Status:** confirmed (size, experiment `exp_welcome.py`, `exp3`); needs live stack (the 413 itself, though Synapse's 65536-byte event limit is well established)
- **Effort:** S

`BroadcastResult.summary()` appends one line per failed room, and each line embeds `str(ApiError)`, which is the full request URL (homeserver, room id, 32-character txn id) plus the response payload. A realistic 429 line is about 340 bytes. Experiment: 300 failures give a 101,954-byte message content, well over the 65,536-byte PDU limit, and the threshold is roughly 190 failures. That is exactly the ONB-03 situation on a few hundred users.

Then `_reply` fails (Synapse answers 413 `M_TOO_LARGE`), the exception escapes to the `SyncPump`, which logs it, and the admin sees *nothing* in the control room after typing `!announce`. The natural reaction is to type it again, which re-sends to every room that did receive the first one. The CLI is unaffected (it prints).

Fix sketch: keep the per-room details in the log and reply with counts plus at most N (say 20) failures and "and K more"; group failures by error class ("312 rate limited, 3 not in room"). Wrap `_reply` so a failed reply is retried once with a minimal text.

### ONB-05 Cutover from the legacy bot re-welcomes every user into a second room

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/onboarding/welcome.py:190-195; legacy/onbot/bot.py:169-208, legacy/onbot/bot.py:397-415
- **Status:** confirmed by code reading (both sides); impact on a real cutover needs live stack
- **Effort:** M

The legacy bot found a user's DM by scanning rooms in the space for its `<reversed>.onbot.direct_room` state event (`_list_user_direct_rooms`) and never wrote `m.direct` (no `m.direct` or account-data use anywhere in `legacy/`). It tracked sent messages by *index* in `send_authentik_user_welcome_messages`. The new bot looks a DM up *only* in `m.direct` and tracks messages by content hash in `welcome_messages_sent`.

So the first run of the new bot against a server the legacy bot managed creates a second room for every user and sends the full welcome sequence again (company-wide page). It leaves the old, encrypted, writable legacy DM behind, and it hits ONB-01 at full scale, because every user needs a room at once. Nothing in `README.md`, `docs/features.md`, `docs/troubleshooting.md` or `CHANGELOG.md` mentions this.

Note that legacy rooms are adoptable: they use the same event type, `room_type: "direct_room"` and `user_id`, and `DirectRoomState` ignores the extra legacy field. They were `trusted_private_chat`-like and encrypted, though, so adopting them gives a writable, encrypted notice board.

Fix sketch: decide and document. Either (a) a one-off adoption step that seeds `m.direct` from rooms carrying `direct_room` state and marks the legacy-index messages as sent, accepting writable legacy rooms, or (b) document that cutover intentionally creates fresh notice boards and re-sends the welcome, and that operators should shorten the message list for the first run. Either way, add a cutover section to the docs.

### ONB-06 Server-side default encryption makes the control room deaf and is never detected

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/rooms/admin.py:137-148, onbot/clients/matrix.py:247-273, onbot/admin/control_room.py:88-96 (`handle_sync` only reads `m.room.message`)
- **Status:** needs live stack
- **Effort:** S

Both the control room and every notice board are created with `preset: private_chat` and no `m.room.encryption`. Synapse's `encryption_enabled_by_default_for_room_type: invite` (or `all`), a common hardening setting, adds `m.room.encryption` to every room created with that preset, and a client cannot opt out at `createRoom`. Then:

* in the control room, admins' clients encrypt their messages, the bot receives `m.room.encrypted`, `handle_sync` skips it, and `!announce` / `!status` / `!help` silently do nothing. The bot's own plaintext help notice still appears, so the room *looks* alive;
* notice boards become encrypted rooms carrying plaintext bot messages (shown with "not encrypted" warnings), contrary to ADR-0009 and `docs/features.md` ("It is unencrypted").

Fix sketch: after creating (and at startup, for the control room) read `m.room.encryption`. If it is present, log an error that names the Synapse setting, refuse to bind the control room handler, and surface it in `healthcheck`. Document the incompatibility in ADR-0009 and `docs/troubleshooting.md`. Live test: set `encryption_enabled_by_default_for_room_type: invite` in the stack and send `!status` from Element or a crypto-capable test client.

## Low

### ONB-07 A user who leaves the notice board can never get back, and broadcasts still count their room as reached

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/onboarding/welcome.py:190-195, onbot/admin/broadcast.py:99-114, onbot/admin/broadcast.py:127-135, onbot/admin/control_room.py:162-168 (`_status`)
- **Status:** confirmed by code reading (join rule `invite` from the `private_chat` preset; send succeeds while the bot is joined); end-to-end needs live stack
- **Effort:** M

"A user who leaves is not dragged back" is implemented, but the other half is missing. The room's join rule is `invite`, the original invite is consumed, and the bot never re-invites. So a user who clicks "Leave" (one click in Element's room list) loses all future announcements for good, with no self-service way back. Broadcast reads only `m.direct`, sends into the room (it succeeds, because the bot is still joined) and counts it in `sent`. `!status` counts it as a managed room. The same holds for erased (deactivated) users, whose entries are never pruned from `m.direct`. So "sent to 420 rooms" can overstate reach considerably, and nobody can tell.

Fix sketch: on the welcome path, when membership is `leave` (not `ban`), send one fresh invite and record `reinvited_at`, so the user can opt back in without being force-joined. In broadcast, skip or separately count rooms whose target user is not joined (one membership read per room, or the admin API members list), and report "N delivered, M not a member". Prune `m.direct` entries when the lifecycle erases an account. Document how an admin restores a user (admin join API).

### ONB-08 A DM the bot can no longer read fails the welcome on every tick forever

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/onboarding/welcome.py:142-143, onbot/onboarding/welcome.py:272-278, onbot/onboarding/listener.py:88-94
- **Status:** plausible (code reading)
- **Effort:** S

On the existing-room path, `_load_direct_state` and `_heal_power_levels` read room state. If the bot is no longer in the room, both raise 403. That happens when the room was purged by hand following `docs/troubleshooting.md` but the `m.direct` entry was not dropped, or when a user of a pre-notice-board room (PL 100) kicked the bot. The same happens with a `ValidationError` from `parse_room_state` on hand-edited content. The listener logs a full traceback and does not cache the failure, so this repeats for that user on every reconcile pass, forever. The user never gets a working notice board, because the stale `m.direct` entry blocks creation of a new one.

Fix sketch: classify the failure. For "not in room" (403 `M_FORBIDDEN`) or 404, drop the `m.direct` entry and let the normal flow create a fresh room (logging a warning once). For unparsable state, log once and treat it as empty state. Rate-limit the per-user error log.

### ONB-09 Welcome delivery is at-least-once, not once

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/onboarding/welcome.py:153-157, onbot/clients/matrix.py:441
- **Status:** plausible (code reading)
- **Effort:** S

ADR-0003 says a message is "sent once per user and never re-sent". The code sends first and records afterwards. If the process dies between the send and `_persist`, or `_persist` exhausts its retries (5xx), the next welcome re-sends that message. The window is one HTTP round trip per message, so this is rare, but it is the opposite of what the ADR promises.

Fix sketch: derive the transaction id deterministically, for example `onbot-welcome-<sha256(room_id + key)[:24]>`, and pass it to `send_text_message` (add an optional `txn_id` parameter). Synapse de-duplicates a repeated `(device, txn_id)` send and returns the original event id (its transaction id table is kept for about a day), so a crash-and-retry within that window becomes idempotent. Adjust the ADR wording to "at most once per transaction window, at least once otherwise".

### ONB-10 Announcements are sent as plain `body` only

- **Severity:** low
- **Category:** bug
- **Location:** onbot/admin/broadcast.py:130, onbot/admin/control_room.py:108 (`content.get("body")`), onbot/cli.py (broadcast)
- **Status:** confirmed by code reading (no `formatted_body` on the broadcast path); rendering in Element needs live stack
- **Effort:** S

Welcome messages go through `render_markdown`. Announcements do not. An admin typing `!announce **Maintenance** tonight, see [status](https://status.example.org)` in Element sends `body` with the markdown source (Element converts markdown locally and puts the source in `body`), and the bot forwards only `body`. Users see literal asterisks and bracket syntax in a company-wide notice. The same applies to `onbot broadcast`.

Fix sketch: in `BroadcastService.broadcast`, pass `formatted_body=render_markdown(message)` (it returns `None` for plain text, keeping plain announcements plain). Optionally, for control room commands, prefer the admin's own `formatted_body` with the `!announce` prefix stripped, but rendering `body` is simpler and consistent with the welcome flow.

### ONB-11 An edit without the `* ` fallback prefix is executed as a new `!announce`

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/admin/control_room.py:98-112, onbot/admin/commands.py:37-55
- **Status:** confirmed (experiment `exp_control.py`)
- **Effort:** S

The handler never looks at `m.relates_to`. An edit gets a new event id and passes the replay guard; it is inert today only because Element prefixes the fallback body with `* `. The prefix is a client convention, not something the server enforces. A client or bridge that sends `body: "!announce ..."` on an `m.replace` event triggers a second company-wide broadcast:

```
broadcast calls: ['Maintenance at 22:00', 'Maintenance at 23:00']
```

(The first edit, with `* `, was ignored. The second, without it, broadcast again.) The opposite is also true: an admin correcting a typo in an `!announce` gets silence, with no hint that edits are not supported.

Fix sketch: ignore any event with `m.relates_to.rel_type == "m.replace"`, and reply once: "edits are not commands; send a new !announce". Consider a short duplicate guard (same text from the same admin within 60 s asks for confirmation), since a double send is a company-wide page.

### ONB-12 An admin who rejects or leaves the control room is re-invited on every reconcile tick

- **Severity:** low
- **Category:** bug
- **Location:** onbot/rooms/admin.py:116-126
- **Status:** confirmed by code reading
- **Effort:** S

`ensure_admins_invited` skips only `join` and `invite`. Membership `leave` (rejected invite or left the room) gets a fresh invite on the next `reconcile_completed`. An allowlisted admin who does not want the room receives a new invite (with a notification) every tick, indefinitely. For a banned admin, `ban` produces a warning with a traceback every tick.

Fix sketch: invite only when membership is `None` (never in the room), or record per-admin "invited at" in the `AdminRoomState` and invite again only when the admin set changes. Treat `ban` as "do not invite" without a traceback.

### ONB-13 The sync join path welcomes anyone seen joining any room, and replays every member on startup

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/onboarding/listener.py:41-51, onbot/onboarding/listener.py:75-78, onbot/clients/matrix.py:486-494, onbot/sync.py:77-84
- **Status:** plausible (code reading)
- **Effort:** M

`extract_joined_users` returns every `join` in `required_state` and the timeline of every room in the sync window, and the listener welcomes each one that is not on `matrix_user_ignore_list`. That bypasses the reconciler's scope filters (`sync_only_users_in_authentik_pathes`, `sync_only_users_of_groups_with_id`, `filter_is_active`). A local account outside the configured scope (or, on a federating group room, a remote user, for whom the admin join returns 400 and hits ONB-02) that joins any room the bot sits in gets a notice board, and from then on every broadcast. Break-glass admins in `admin_user_ids` who are not Authentik users get one as soon as they join the control room.

Because `required_state` is `m.room.member *`, the first sync after every restart hands the listener every member of up to 1001 rooms. It welcomes them one by one (3 reads each while `_welcomed` is empty), duplicating the reconciler's own first pass. Since `SyncPump` calls handlers sequentially, the control room handler waits behind all of it, so a command sent right after a restart can take minutes to be answered. Conversely, a long `!announce` blocks sync-driven onboarding for its duration.

Fix sketch: let the sync path only *accelerate* users the reconciler already mapped. Keep the set of mapped MXIDs from the last pass and ignore joins of anyone else. Skip `required_state` membership on the very first slice (it is replay, not news). Consider running the control room handler as its own task, so neither consumer blocks the other.

### ONB-14 An expired sliding-sync `pos` is retried forever

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/sync.py:61-74, onbot/clients/matrix.py:469-505
- **Status:** needs live stack (overlaps RUN; listed here because it disables the control room)
- **Effort:** S

MSC4186 lets the server reject a `pos` it no longer knows (HTTP 400 `M_UNKNOWN_POS`, for example after the connection expired). `SyncPump.run` catches the error, sleeps 5 s and retries with the *same* `self._pos`, forever. From then on no command in the control room is ever seen and sync-driven onboarding stops (the reconciler signal path still works), with an error log every 5 s and no other symptom. The healthcheck does not cover the pump.

Fix sketch: on a 400 whose errcode is `M_UNKNOWN_POS`, reset `self._pos = None`. The replay that follows is already safe (control room floor and ring buffer, idempotent welcome). Live test: restart Synapse, or expire the connection, while the bot holds a `pos`, then send `!status`.

### ONB-15 Avatars are keyed by URL only

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/avatar.py:64-70, onbot/media.py:131-137, onbot/onboarding/welcome.py:233-241 (`_maybe_set_avatar`), onbot/reconciler/engine.py:249
- **Status:** confirmed by code reading
- **Effort:** M

Three different idempotency mechanisms exist, and all of them key on the source URL:

* the bot profile: an account-data record `{source_url, mxc}`. If the record matches the configured URL and the profile, the bot returns without fetching. The byte comparison only runs when the record is missing;
* group rooms: `avatar_source_url` in the room state (engine.py:249);
* notice boards: set once at creation and never revisited.

Consequences: replacing the image behind an unchanged URL (`https://company.org/logo.png` after a rebrand) never reaches the bot profile or any room, even after restarts. Changing `bot_avatar_url` updates the profile and group rooms but leaves every existing notice board with the old picture. A notice board created while the avatar URL was unreachable stays without an avatar forever. `MediaUploader.fetch` also accepts any content type and any size (an HTML error page served with 200 becomes the avatar); the size and SSRF angle belongs to the security review.

Fix sketch: one `ensure_avatar(target, url)` helper that stores `{source_url, sha256, mxc}` and re-fetches with a conditional GET (ETag/Last-Modified) at most once per process start, used for the profile, group rooms and notice boards alike. Reject non-`image/*` responses.

### ONB-16 Room provisioning is implemented three times, with the marker written after `createRoom`

- **Severity:** low
- **Category:** simplification
- **Location:** onbot/onboarding/welcome.py:190-206, onbot/rooms/admin.py:132-155, onbot/reconciler/engine.py:275-280, onbot/clients/matrix.py:153-273
- **Status:** confirmed by code reading
- **Effort:** M

The notice board, the control room and group/lobby rooms each do their own "create, then write the onbot marker state, then optionally avatar, topic and space link". All three write the marker in a *separate* request after `createRoom`, which opens the crash window behind ONB-02 (and the analogous window for group rooms, see the REC review). They also differ in what they converge afterwards: the admin room repairs its topic, notice boards never do, group rooms converge name, topic and avatar.

Fix sketch: a single `ManagedRoomSpec` (preset, power-level override, name, topic, encryption, `m.federate`, marker content, optional space parent) and one `create_managed_room(spec)` that puts the marker into `initial_state`, so a room either does not exist or carries its bookkeeping. A shared `ensure_room_attributes(room_id, spec)` can then cover topic and avatar drift for all three kinds. `create_direct_message_room` collapses into a spec.

### ONB-17 The control room alias is trusted without checking the `admin_room` marker

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/rooms/admin.py:88-99, onbot/rooms/admin.py:190-196
- **Status:** plausible (code reading; overlaps SEC)
- **Effort:** S

The module docstring says the marker makes the room "recognisably bot-managed rather than some room that happens to share the alias", but `ensure()` takes whatever `#onbot-admin` resolves to, and `_load_state` returns a fresh default when the marker is absent. If the alias points at a room the bot is in but did not create (an alias moved by a server admin, or a group room that got the alias by mistake), the bot treats that room as its control room. It rewrites its topic, posts and pins the help there, invites all admins into it, and executes `!` commands from it. If the bot is not in the room, `ensure()` fails and the control room is silently off (logged once at startup).

Fix sketch: after resolving, require the `admin_room` marker (or that the bot is the room's creator). Otherwise refuse with an error naming the alias, rather than adopting the room.

## Info

### ONB-18 Message identity is the raw configured text

- **Severity:** info
- **Category:** docs
- **Location:** onbot/onboarding/welcome.py:68-74 (`_message_key`), docs/features.md:304-306
- **Status:** confirmed by code reading
- **Effort:** S

This is intended and documented ("editing one message re-sends *that* message to everyone"). It is still sharper than the documentation suggests. The hash covers the exact string, with no normalisation in `config.py`. Fixing a typo, switching a YAML block from `|` to `|-` (trailing newline), or re-indenting a list re-sends that message to every user ever onboarded, out of order and long after the fact. An operator cannot correct a message without paging the whole company.

Fix sketch: allow an optional stable `id` per message (`- id: intro\n  text: ...`), keyed by id when present and by the hash otherwise. Normalise trailing whitespace before hashing. State in the config field description, not only in features.md, that an edit re-sends.

### ONB-19 `todo.md` is stale for Session A and its header; broadcast silently needs welcome messages

- **Severity:** info
- **Category:** docs
- **Location:** todo.md:1-16, todo.md:103-202, onbot/onboarding/welcome.py:128-130, docs/features.md:457-496
- **Status:** confirmed (code and git log)
- **Effort:** S

Session A is not marked done, but items 1, 2, 3 and 5 shipped in `c3f6fe8` ("make welcome room read only notice room"): `private_chat` plus the override, force-join with `force_joined_at`, name/topic/avatar, and power-level healing. Item 4, the `onbot recreate-dm-rooms` command with dry-run, `--yes` and help text warning about re-sends, was never built. `docs/troubleshooting.md` describes a manual purge procedure instead. The header still says "Two self-contained work packages" above five sessions (R, A, B, C, D). Session B is accurately marked done and matches the code, including persist-before-execute and the ring buffer (bounded at 200, empty on first start, persistence failure logged and ignored).

Separately: `welcome_user` returns before creating a room when `welcome_new_users_messages` is empty, so a deployment that wants announcements but no welcome text has no notice boards, and `!announce` reports "sent to 0 rooms". The broadcast documentation does not say so.

Fix sketch: strike Session A through with a note that only `recreate-dm-rooms` remains (or move that item to Deferred), and fix the header. Either create notice boards regardless of the message list, or document the dependency in the `admin_room` section.
