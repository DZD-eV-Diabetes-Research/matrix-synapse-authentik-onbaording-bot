# 01 Security of the application code

Scope: tokens and secrets (`auth/`, `clients/base.py`, `logging.py`, `config.py`), the admin control room
(`admin/`, `rooms/admin.py`), HTML rendering (`formatting.py`, `onboarding/`), remote fetching (`media.py`,
`avatar.py`), and every path where Authentik or Matrix data drives a privileged action (`identity.py`,
`reconciler/`, `lifecycle/`). The token and logging hygiene is good and HTML rendering is safe. The real
problems are in the trust boundaries: offboarding can be reversed, rooms are adopted by alias alone, and
Authentik group attributes reach the network and the Matrix auth rules with no validation.

| ID | Severity | Title |
|---|---|---|
| SEC-01 | high | Deleting a disabled Authentik user unlocks their MAS account and abandons offboarding |
| SEC-02 | high | Rooms are adopted by canonical alias alone, so any local user can squat a future room |
| SEC-03 | medium | Group avatar URL is a full-read SSRF with no size limit and no total timeout |
| SEC-04 | medium | One failing room operation aborts the pass before offboarding runs |
| SEC-05 | medium | `chatroom_params` is on by default and lets a group editor plant an unremovable room owner |
| SEC-06 | medium | Anyone who joins a room the bot is in gets a notice board and every future broadcast |
| SEC-07 | low | MXID comes from a mutable attribute: a rename escapes offboarding, collisions are silent |
| SEC-08 | low | URL path segments are not encoded; a valid localpart can redirect a MAS lock to another user |
| SEC-09 | low | Admin allowlist trusts Authentik's server-side group filter without re-checking |
| SEC-10 | low | OAuth2 token provider refreshes without a lock |
| SEC-11 | info | Secrets are plain `str`; internal Authentik URL is written into every room's state |
| SEC-12 | info | Control room: replay buffer is written before the allowlist check; edit fallbacks |

Checked and found sound (no finding): bearer tokens never appear in URLs, and neither httpx (INFO) nor
httpcore (DEBUG) logs the `Authorization` header (confirmed with a local HTTP server at DEBUG level, see
SEC-11); pydantic validation errors truncate `input_value`, so a config error does not print the tokens
(confirmed); `render_markdown` escapes raw HTML and drops `javascript:` links, including when a
placeholder value contains `<b>` (confirmed); `!announce` is sent as plain `body` only; event `sender` is
set by the server and the control room is created with `m.federate: false`, unencrypted, `invite`/`kick`/
state at 100.

---

## High

### SEC-01 Deleting a disabled Authentik user unlocks their MAS account and abandons offboarding

- **Severity:** high
- **Category:** security
- **Location:** onbot/reconciler/engine.py:629-663, onbot/lifecycle/accounts.py:75-79, onbot/lifecycle/accounts.py:266-271, onbot/lifecycle/accounts.py:167-170
- **Status:** confirmed (simulation with the real `AccountLifecycleManager` and `MasLifecycleEffectors` against a fake MAS client: mark, then lock, then `unlock` as soon as the user is absent from the disabled list). MAS behaviour on unlock needs live stack.
- **Effort:** M

`_gather_orphaned_mxids` only considers users returned by `list_users(filter_is_active=False)`. A user
that is no longer in that list is "not orphaned", and `decide_account_action` returns `reenable` for any
MXID that has ledger state and is not orphaned. `reenable` runs even in dry-run and, with MAS configured,
calls `unlock_user`.

Scenario (a very common offboarding sequence): HR disables `alice` in Authentik. After
`deactivate_after_n_sec` the bot locks her MAS account. Weeks later an admin deletes the Authentik user
to clean up. On the next pass `alice` is not in the disabled list, not in the active list, the ledger
still has her entry, so the bot unlocks her MAS account and clears the ledger. Per the MAS admin API
documentation, locking does not invalidate sessions, they "work again as soon as they get unlocked", so
a phone that was never logged out regains access, and nothing will ever lock it again because the
ledger entry is gone. The same happens when the user is renamed, moved out of
`sync_only_users_with_authentik_attributes` (offboarding scripts often strip opt-in attributes), or
when the mapping attribute is removed.

Two related gaps: deleting an Authentik user *without* disabling first is never offboarded at all,
although the config description of `deactivate_disabled_authentik_users_in_matrix` promises "disabled
or deleted" (`config.py:416`, `accounts.py:3`). And in dry-run, the "restorative" `reenable` unlocks a
MAS account that a human locked for unrelated reasons, because the ledger cannot tell who locked it.

Fix sketch: only `reenable` when the MXID is positively seen *active* this pass (`mxid in
active_mxids`), never on mere absence; keep the ledger entry (and keep progressing to erase) for users
that vanished; only call `unlock` when the ledger records that the bot itself locked
(`disabled_user_timestamp` set and not dry-run). To cover direct deletion, persist the set of MXIDs the
bot has ever mapped and treat "previously mapped, Matrix account exists, no Authentik user any more" as
orphaned. Overlaps with the lifecycle area (LIF); listed here because the effect is a reversed lockout.

### SEC-02 Rooms are adopted by canonical alias alone, so any local user can squat a future room

- **Severity:** high
- **Category:** security
- **Location:** onbot/reconciler/rooms.py:217-231, onbot/reconciler/engine.py:210-217, onbot/reconciler/engine.py:271-300, onbot/rooms/admin.py:92-99, onbot/clients/matrix.py:327-343
- **Status:** confirmed by code reading for the denial of service; force-join into the attacker's room needs live stack
- **Effort:** M

Group rooms and lobbies are paired with existing rooms purely by `canonical_alias` from the server-wide
admin room list; the space likewise (`_resolve_space`), and the control room by `resolve_room_alias`.
Nothing checks that the bot created the room or that it carries the `onbot.*` marker (the `rooms/admin.py`
docstring says the marker makes the room "recognisably bot-managed", but `ensure()` never reads it).
Synapse's default `alias_creation_rules` let every local user create any free alias.

Scenario: a regular Matrix user creates a room with alias `#<group-alias>-lobby:server` (the lobby alias
is the group alias, which members can see, plus the fixed `-lobby` suffix) or with the alias of a group
that is about to be opted into chat (predictable with `alias_prefix` or `attributes.chatroom_alias`).
When the bot reaches that group it adopts the attacker's room:

1. The bot is not a member, so `_converge_avatar` gets HTTP 403 from `get_room_state`, which only
   swallows 404. The exception leaves `reconcile_once`, and that repeats on every tick: no new user is
   added to any room and offboarding never runs (SEC-04). Same for the control room: `_ensure_topic`
   raises and the bot "continues without" a control room.
2. If the attacker also sets `history_visibility: world_readable` and `join_rule: public`, the state
   reads succeed and Synapse's admin join API joins public rooms directly, so the bot force-joins the
   whole group (and, for a lobby, `visitor_lobby_inject_group_members` defaults to true) into a room
   where the attacker is creator. Under room version 12 the attacker has infinite power there and reads
   everything the group posts, including keys for encrypted messages, because they are a member.

Fix sketch: before adopting a room, read the Room Details admin API (works without membership) and
require `creator == bot_user_id` (or the `m.room.create` sender) plus the onbot marker for the expected
type; otherwise log an error naming the room and skip that group without aborting the pass. Document a
Synapse `alias_creation_rules` entry that reserves the bot's alias namespace.

---

## Medium

### SEC-03 Group avatar URL is a full-read SSRF with no size limit and no total timeout

- **Severity:** medium
- **Category:** security
- **Location:** onbot/media.py:25, onbot/media.py:38-47, onbot/reconciler/rooms.py:117-122, onbot/reconciler/engine.py:234-260
- **Status:** confirmed by code reading; reachable targets depend on the deployment network
- **Effort:** M

`chatroom_avatar_url` is read from a group's Authentik attributes (on by default), and the docs address
that attribute to "group owners" (`docs/authentik-groups.md:8`). `MediaUploader.fetch` GETs it with
`follow_redirects=True`, no scheme or host restriction, no size limit (`response.content` buffers the
whole body), and trusts the remote `Content-Type`. The bytes are then uploaded to the media repo and set
as the room's `m.room.avatar`, so every room member can download them.

Scenario: a group editor sets `chatroom_avatar_url: http://169.254.169.254/latest/meta-data/...` or a
public URL that redirects to `http://mas:8080/...`, `http://authentik-server:9000/-/metrics/` or any other
unauthenticated internal endpoint reachable from the bot's container. The response becomes media in the
group room and the URL is recorded in the room state. Two availability variants: a multi-gigabyte URL
makes the bot buffer it all in memory; a server that trickles one byte every 29 seconds never trips
httpx's per-operation 30 s timeout, and because the reconcile pass is sequential it stalls every room and
offboarding (and the failure is retried on every pass, since nothing is recorded on failure).

Fix sketch: allow only `https` (and optionally an operator allowlist of hosts), resolve and reject
private, loopback and link-local addresses on every hop (or disable redirects), stream with a hard byte
cap (a few MB) and an overall deadline, and require an `image/*` content type that matches the bytes.
The bot avatar URL from config can share the same code path.

### SEC-04 One failing room operation aborts the pass before offboarding runs

- **Severity:** medium
- **Category:** security
- **Location:** onbot/reconciler/engine.py:133-154, onbot/reconciler/engine.py:420-421, onbot/reconciler/engine.py:462-468, onbot/reconciler/engine.py:575-590
- **Status:** confirmed by code reading (no per-room `try`, `_converge_lifecycle` is the last statement)
- **Effort:** S

`reconcile_once` runs room creation, obsolete rooms, space, membership and power levels, and only then
`_converge_lifecycle`. None of the per-room steps is isolated, and `run()` just logs and waits for the
next tick. Any input that makes one room operation fail permanently therefore disables offboarding for
the whole server, silently apart from a repeating stack trace.

Triggers available to a group-attribute editor: `chatroom_params: '{"room_version": "nope"}'` (createRoom
400 every tick), a power-level attribute outside the canonical JSON integer range or above the bot's level
in a pre-v12 room (403 or 400 on `set_room_power_levels`), or an `additional_creators` member who joins
(the bot can never kick a creator, so `kick_user` 403s on every pass, see SEC-05). Triggers available to
any local Matrix user: SEC-02.

Fix sketch: run `_converge_lifecycle` first (it only needs `matrix_users` and the mapped users) or in its
own `try`; wrap each room's convergence so one room's `ApiError` is logged with the room and group and
the loop continues; expose "last pass with errors" in `!status` and the healthcheck. Overlaps with the
reconciler area (REC); listed here because the consequence is that disabled users keep their sessions.

### SEC-05 `chatroom_params` is on by default and lets a group editor plant an unremovable room owner

- **Severity:** medium
- **Category:** security
- **Location:** onbot/reconciler/rooms.py:104-115, onbot/clients/matrix.py:196-215, onbot/config.py:851-867, onbot/reconciler/power_levels.py:252-272
- **Status:** confirmed by code reading for the pass-through; the room version 12 creator semantics need live stack
- **Effort:** S

Deliberate and documented as an "unvalidated escape hatch" (`docs/authentik-groups.md:182-296`). I am
challenging the default, not the existence of the feature. `matrix_room_create_params_from_authentik_attribute`
defaults to `attributes.chatroom_params`, so anyone who can edit a group's attributes in Authentik writes
an arbitrary `createRoom` body for the room the bot creates for that group. That is a larger capability
than "group owner" suggests:

- `{"creation_content": {"additional_creators": ["@mallory:server"]}}` (room version 12, the default)
  makes Mallory a creator with infinite, immutable power. The bot cannot demote or kick her even after
  she leaves the group or is offboarded, and the failed kick aborts every pass (SEC-04).
- `{"power_level_content_override": {"users": {"@mallory:server": 100}}}` for an MXID that is not a
  group member is never withdrawn, because `merge_power_levels` only touches managed MXIDs.
- `{"preset": "public_chat", "visibility": "public"}` publishes a confidential group's room in the server
  directory; the bot never converges join rules for group rooms (`join_rules.py` returns `None`), so this
  is permanent. `room_version`, `m.federate` and `initial_state` are equally irreversible.

Fix sketch: default the attribute to `null` so the operator opts in, and when enabled accept only an
allowlist of keys (`preset` limited to `private_chat`/`public_chat`, `visibility`, `creation_content.m.federate`,
`history_visibility` via `initial_state`), rejecting `additional_creators`, `power_level_content_override.users`,
`invite` and `room_version`.

### SEC-06 Anyone who joins a room the bot is in gets a notice board and every future broadcast

- **Severity:** medium
- **Category:** security
- **Location:** onbot/onboarding/listener.py:41-51, onbot/onboarding/listener.py:83-95, onbot/onboarding/welcome.py:250-264, onbot/admin/broadcast.py:295-310
- **Status:** confirmed by code reading for local users; the federated case needs live stack
- **Effort:** S

`extract_joined_users` collects every `join` membership event in every synced room, and `_maybe_welcome`
filters only the bot and `matrix_user_ignore_list`. There is no check that the MXID is on this server or
belongs to a mapped Authentik user. `WelcomeService` then creates a DM, invites the user, writes them into
the bot's `m.direct`, and `BroadcastService.target_rooms` uses `m.direct` as the recipient list.

Scenario: group rooms are created without `m.federate: false` by default. A group member invites
`@someone:partner.org` into the group room. The bot sees the join, sends the welcome messages (intranet
links, space link) to the external account and adds the DM to `m.direct`. The reconciler kicks the
outsider on the next pass, but the notice board stays, so every later `!announce` and `onbot broadcast`
("maintenance tonight", "new VPN credentials process", ...) goes to an external homeserver. Local accounts
that are not in Authentik (service accounts, MAS-only users) are enrolled the same way.

Fix sketch: welcome only MXIDs in the set the last reconcile mapped (the `user_synced` path already
provides it), or at minimum require the `:<server_name>` suffix and an existing local account; filter
broadcast targets against the same set; drop `m.direct` entries for users no longer mapped.

---

## Low

### SEC-07 MXID comes from a mutable attribute: a rename escapes offboarding, collisions are silent

- **Severity:** low
- **Category:** security
- **Location:** onbot/identity.py:34-49, onbot/reconciler/engine.py:172-185, onbot/reconciler/engine.py:648-663, onbot/admin/admins.py:285-312
- **Status:** plausible (depends on Authentik's "Allow users to change username" setting, which I believe is off by default)
- **Effort:** L

MAS fixes a user's localpart once, at first login, and links the account to the upstream `sub`. Onbot
recomputes the MXID on every pass from the current value of `authentik_username_mapping_attribute`
(default `username`). If users may change their own username, an employee who expects to be let go can
rename themselves to `zzz` first: their real account `@alice` is then unmapped (kicked from group rooms
but keeps DMs, other rooms and sessions), and when HR disables them the orphan finder computes `@zzz`,
which does not exist in Synapse, so `@alice` is never locked. Separately, two Authentik users that map to
the same MXID (`attributes.matrix_name` is not unique) are resolved by page order (`by_mxid[mxid] = user`,
last one wins), so one person's groups, power levels or bot-admin membership land on another's account
without a warning.

Fix sketch: key identity on something immutable. MAS's admin API exposes upstream OAuth links by subject,
so the bot can resolve Authentik `uid`/`sub` to the MAS user and its real localpart; at minimum, record
the pk to MXID binding in the ledger, alert when it changes, and refuse (fail closed) on duplicate MXIDs.

### SEC-08 URL path segments are not encoded; a valid localpart can redirect a MAS lock to another user

- **Severity:** low
- **Category:** security
- **Location:** onbot/clients/mas_admin.py:50-59, onbot/clients/synapse_admin.py:195-283, onbot/clients/matrix.py:306-465, onbot/clients/base.py:256-261
- **Status:** confirmed (httpx normalises dot segments: `users/by-username/x/../victim` is sent as `users/by-username/victim`); end-to-end exploit plausible
- **Effort:** S

User IDs, room IDs, event types and state keys are interpolated into paths without `quote(..., safe="")`
(only `resolve_room_alias` and `matrix_to_link` quote), and `compute_mxid` does not validate the localpart
grammar. `/` and `.` are legal in Matrix localparts. For a disabled user whose mapped localpart is
`x/../victim`, `MasLifecycleEffectors._resolve` looks up `by-username/victim`, receives the victim's ULID
and then locks and later deactivates the wrong MAS account. Synapse admin paths behave the same way
(`v2/users/@x/../../users/@victim:server/devices` normalises to the victim's device list), although there
an `@` would be needed inside the localpart, which Matrix forbids. Precondition: the attacker controls their
own mapped value and MAS accepts the localpart. Related footgun: `_build_url` forwards any `http(s)://`
path verbatim with the bot's bearer token; no current caller passes untrusted input there, but it is one
f-string away.

Fix sketch: percent-encode every interpolated segment (`quote(x, safe="")`), validate localparts against
`[a-z0-9._=\-/+]+` and reject `.`/`..` segments in `compute_mxid`, and restrict absolute URLs in
`_build_url` to the client's own origin.

### SEC-09 Admin allowlist trusts Authentik's server-side group filter without re-checking

- **Severity:** low
- **Category:** security
- **Location:** onbot/admin/admins.py:363-384, onbot/clients/authentik.py:36-71
- **Status:** plausible (hardening; no Authentik version is known to ignore `groups_by_pk`)
- **Effort:** S

`ApiClientAuthentik.list_users` documents that a server which does not recognise a filter "drops it and
answers with every user", and `global_admins.py:121-124` re-checks locally for exactly that reason.
`AdminResolver.refresh` does not: it passes `groups_by_pk` and grants bot admin to every returned user.
If the parameter is ever renamed or ignored (API change, proxy rewriting query strings), every active
Authentik user can `!announce` to the company and is invited into the control room. Fix: keep only users
whose `groups` (pk list in the user object) intersect `authentik_group_pks_granting_bot_admin`, and log
loudly if the server returned anyone else. The same applies to `sync_only_users_of_groups_with_id`.

### SEC-10 OAuth2 token provider refreshes without a lock

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/auth/token_provider.py:91-133
- **Status:** plausible
- **Effort:** S

One provider is shared by the admin and CS clients, and the sync long poll, the reconciler and a
broadcast fan-out (5 concurrent sends) all call `get_token()`. At expiry every waiter calls `_refresh()`
at once. With the `refresh_token` grant they all present the same refresh token; MAS rotates refresh
tokens, so all but one fail, `_token_request` drops the refresh token and raises `OAuth2TokenError`,
which is neither an `ApiError` nor a `TransportError` and therefore is not retried: those requests fail
(a broadcast reports failures, a reconcile pass aborts). It self-heals on the next call through a
`client_credentials` exchange. Fix: guard `_refresh` with an `asyncio.Lock` and re-check expiry inside it.

---

## Info

### SEC-11 Secrets are plain `str`; internal Authentik URL is written into every room's state

- **Severity:** info
- **Category:** security
- **Location:** onbot/config.py:88, onbot/config.py:148, onbot/config.py:305, onbot/config.py:393, onbot/reconciler/state.py:39-41, onbot/reconciler/engine.py:408-412
- **Status:** confirmed (`repr(SynapseServer(...))` prints `bot_access_token='...'`; a DEBUG run against a local HTTP server showed no token in any log line)
- **Effort:** S

`bot_access_token`, both `client_secret`s and `api_key` are `str`, so any future `log.debug("%r", config)`,
error reporter that captures locals, or `model_dump()` prints them. Today nothing does, and pydantic
truncates `input_value` in validation errors. Switching to `SecretStr` (with `.get_secret_value()` at the
three construction sites) costs little. Separately, every onbot state event (`authentik_server`) and the
kick reason in `_disable_room` carry `authentik_server.url`, which the config docs recommend be an
internal URL; every room member can read it. Prefer a configurable public label or omit it.

### SEC-12 Control room: replay buffer is written before the allowlist check; edit fallbacks

- **Severity:** info
- **Category:** security
- **Location:** onbot/admin/control_room.py:108-124, onbot/admin/commands.py:209-226
- **Status:** confirmed by code reading
- **Effort:** S

`_remember` runs for every parsed `!` message before the sender is checked, so a non-admin member (ADR-0010
deliberately keeps demoted admins in the room) can push 200 entries and evict the real commands from the
persisted ring buffer, and costs one account-data write plus one reply per message. That only matters
together with a clock skew that defeats the age floor, so the at-most-once guarantee degrades to the age
check alone. Move `_remember` after the allowlist check. Also, `m.replace` edits are parsed from their
fallback `body`: Element prefixes it with `* `, which keeps it inert, but a client that omits the prefix
turns an admin's typo fix into a second company-wide `!announce`. Ignore events with
`m.relates_to.rel_type == "m.replace"`.
