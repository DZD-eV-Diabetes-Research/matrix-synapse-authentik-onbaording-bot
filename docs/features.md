# Onbot features

This page explains, in plain language, what Onbot can do for you. It is written for two readers:

- **Users** — people who log into the chat and end up in rooms. You do not configure anything; this
  tells you what the bot is doing on your behalf and why a room appeared.
- **Admins** — people who run Onbot. Each feature says what it is for, when to turn it on, and where
  to find the exact settings.

Every feature is optional unless noted, and each links to its settings in the
[configuration reference](CONFIG_REFERENCE.md). Onbot's guiding idea: **Authentik is the source of
truth, and Onbot mirrors it into Matrix** — groups become rooms, membership becomes room membership,
roles become power levels. You manage people in Authentik; the chat follows.

## Feature overview

- **[Visitor lobby](#visitor-lobby)** — an open "front door" room beside a private group room, that
  anyone in the wider space can find and join.
- **[Group rooms](#group-rooms)** — each Authentik group becomes a Matrix room.
- **[Membership sync](#membership-sync)** — group membership becomes room membership, added and
  removed automatically.
- **[Power levels from Authentik](#power-levels-from-authentik)** — roles and group attributes decide
  who is a room moderator or admin.
- **[Parent space](#parent-space)** — all the group rooms are gathered under one space in the user's
  room list.
- **[Welcome messages](#welcome-messages)** — a new user gets a friendly 1:1 notice board with
  getting-started messages.
- **[Room names, topics and icons](#room-names-topics-and-icons)** — kept in step with Authentik.
- **[Offboarding](#offboarding)** — when someone is disabled in Authentik, their chat access is wound
  down safely, with a dry-run/audit default.
- **[Admin control room and broadcasts](#admin-control-room-and-broadcasts)** — announce a message to
  everyone, from a room or the command line.
- **[Cleaning up orphaned rooms](#cleaning-up-orphaned-rooms)** — when a group disappears, its room is
  closed.

---

## Visitor lobby

**In one sentence:** the private group room stays exactly as private as it is today, and beside it
Onbot can keep a second, open room — its *lobby* — that anyone in the wider space can find and walk
into, and that nobody is ever kicked out of.

### The problem it solves

A group room like *Düsseldorf* is private: only members of the Authentik *Düsseldorf* group are in
it, and outsiders cannot see it. That is usually what you want — but sometimes you want the group to
be **reachable**. Someone from another team should be able to find *Düsseldorf*, drop in, and ask a
question, without being made a full member of the group.

You cannot simply "open up" the existing room. A Matrix room remembers its history, and by default a
newcomer who joins can scroll all the way back to the day it was created — through months of
conversations nobody held with visitors in mind. Opening a live room exposes its past.

A lobby avoids that completely, because **it is a brand-new, empty room**. There is no history to
leak. The group's real working room is left untouched; the lobby sits next to it as an open door.

### What a user sees

- **If you are in the group** (e.g. you are in Authentik's *Düsseldorf* group): you are in **both**
  rooms — *Düsseldorf* (your private working room) and *Düsseldorf (Lobby)* (where visitors may turn
  up). Onbot puts you in the lobby automatically so a visitor who walks in finds somebody there.
- **If you are not in the group** but you are in the shared space: in the space's room list you see
  **the lobby, and only the lobby** — not the private group room. You can join the lobby yourself,
  with one click, no invitation needed. You **cannot** see or join the private group room.
- **Nobody is ever kicked from a lobby.** Once you are in, you stay — even if you were never in the
  group, and even if you later leave the group. (The one exception is if your whole account is
  deactivated, which removes you from every room.)

The lobby is named after the group with a suffix, so it is obvious which room is which:
*Düsseldorf* is the working room, *Düsseldorf (Lobby)* is the front door. Its topic spells out the
arrangement, so a visitor is never confused about where they have landed.

### What an admin should know

- **It is off by default.** A lobby is a deliberate choice, not something that appears on upgrade.
- **Turn it on per deployment or per group.** Set `visitor_lobby_enabled` on the default room
  settings to give every group a lobby, or let a group owner opt in from Authentik by setting the
  group attribute named in `matrix_room_visitor_lobby_from_authentik_attribute` — no config redeploy
  needed.
- **A lobby needs the parent space.** The lobby is joinable precisely by members of the space, so the
  space feature (`create_matrix_rooms_in_a_matrix_space`) must be on. Onbot refuses to start if you
  enable a lobby without a space, rather than quietly creating a room nobody can join.
- **Lobbies are unencrypted by default**, on purpose — and this is *weaker* than the group room's
  default. A room open to the whole space gains little from encryption, while encryption would greet
  every visitor with a screen of "unable to decrypt". You can turn it on
  (`visitor_lobby_end2end_encryption_enabled`), but only before the lobby is first created —
  encryption can never be switched off again.
- **The group's members are seeded into the lobby** by default, so it is not a ghost town. If you run
  a large group and would rather staff an empty lobby deliberately, turn
  `visitor_lobby_inject_group_members` off.
- **One honest limitation.** The lobby hides the group room's *contents*, not the bare fact that it
  exists. A determined person can learn that a room with that ID exists (though they still cannot
  join it, read it, or see its members). If even the existence must be secret, the answer is a second
  space, not a lobby — see [troubleshooting.md](troubleshooting.md#the-lobby-room-id-leak-and-why-a-lobby-is-a-second-room-not-a-visitable-first-one).

**Settings:** all the `visitor_lobby_*` fields under
[`matrix_room_default_settings`](CONFIG_REFERENCE.md), plus
`matrix_room_visitor_lobby_from_authentik_attribute`. The full design rationale is in
[ADR-0012](adr/0012-lobby-rooms.md).

---

## Group rooms

**In one sentence:** every Authentik group Onbot is told to mirror becomes one Matrix room, created
once and then kept as the place that group meets.

### The problem it solves

You already decide who belongs together in Authentik — a team, a site, a project is a group there.
Building the same set of rooms by hand in Matrix, and keeping it current as groups are created and
retired, is exactly the busywork Onbot removes: you manage the group, and the room follows. Because
not every group is meant to be a chat room, Onbot only turns a group into a room when the group opts
in, so your identity directory and your chat rooms need not be the same list.

### What a user sees

- A room appears in your list for each mirrored group you belong to. You did not create it or ask to
  join — it is simply there because you are in the group.
- The room is named after the group, and it is **private**: only members of that Authentik group are
  in it, and outsiders cannot see or join it. Inside, you chat normally.
- You never manage the room's membership yourself; that is what the group is for.

### What an admin should know

- **It is on by default** (`sync_matrix_rooms_based_on_authentik_groups.enabled`), and by default
  *every* group becomes a room. That is rarely what you want on a real directory, so pick a way to
  opt groups in.
- **Choose which groups become rooms.** Any of three filters selects a subset: a custom group
  attribute (`only_groups_with_attributes`, e.g. `is_chatroom: true`), a name prefix
  (`only_for_groupnames_starting_with`), or being a direct child of a parent group
  (`only_for_children_of_groups_with_uid`). `authentik_group_id_ignore_list` excludes named groups
  outright, before every other filter.
- **A room is created once and then reused.** By default its permanent Matrix alias is derived from
  the group's stable primary key, not its name (`matrix_alias_from_authentik_attribute`) — precisely
  so that renaming the group in Authentik does not strand the room, because a Matrix alias can never
  be changed after the room exists.
- **Rooms are end-to-end encrypted by default** (`end2end_encryption_enabled`). Like all encryption
  in Matrix this can never be turned off again once a room has it, so decide before the room is
  created. You can override this and other settings for a single group through
  `per_authentik_group_pk_matrix_room_settings`.

**Settings:** [`sync_matrix_rooms_based_on_authentik_groups`](CONFIG_REFERENCE.md) chooses which
groups become rooms; [`matrix_room_default_settings`](CONFIG_REFERENCE.md) shapes each room.

---

## Membership sync

**In one sentence:** whoever is in the Authentik group is put into its room, and — by default —
whoever leaves the group is removed from its room, so room membership stays a faithful mirror of the
group.

### The problem it solves

A room that stands for a group is only useful if being in the group actually puts you there and
leaving actually takes you out. Kept by hand, that mirror drifts the moment somebody forgets an
invite or a kick. Onbot makes Authentik group membership the single lever: pull it, and the room
follows.

### What a user sees

- Join a group in Authentik and you appear in its room, with no invitation to accept — you are simply
  a member.
- Leave the group and you are removed from the room (unless your admin has turned removals off).
- You are never asked to manage any of this.

### What an admin should know

- **Who is considered at all** is narrowed by the user filters: `sync_only_users_in_authentik_pathes`,
  `sync_only_users_with_authentik_attributes`, and `sync_only_users_of_groups_with_id`. Only *active*
  Authentik users are synced, and `authentik_user_ignore_list` keeps named accounts (service,
  break-glass) out of chat entirely.
- **The identity match is the thing to get right.** Onbot maps each Authentik user to a Matrix ID
  using `authentik_username_mapping_attribute`. On a homeserver fronted by the Matrix Authentication
  Service this *must* agree with the localpart the login flow produces, or the computed Matrix IDs
  name accounts that do not exist and nobody is ever added. If users never land in their rooms, this
  is almost always why — see the first row of [troubleshooting.md](troubleshooting.md) and the
  [configuration guide](configuration.md#the-mxid-localpart-contract).
- **Removals are gated by a switch.**
  `kick_matrix_room_members_not_in_mapped_authentik_group_anymore` is on by default; turn it off to
  make Onbot *add-only*, so it never kicks anyone and users keep rooms after losing the group that
  granted them.
- **Matrix-only accounts are protected.** `matrix_user_ignore_list` shields server admins, other
  bots and bridge users from a sync that would otherwise see them as unknown to Authentik and remove
  them; the bot's own account is always protected.
- Membership is reconciled on a schedule *and* whenever Authentik changes, so it also quietly repairs
  a member somebody kicked by hand inside Matrix.

**Settings:** all the fields under
[`sync_authentik_users_with_matrix_rooms`](CONFIG_REFERENCE.md).

---

## Power levels from Authentik

**In one sentence:** an Authentik group can carry an attribute that sets its members' power level —
their moderator or admin rank — in the room, and Authentik superusers become room admins
automatically.

### The problem it solves

Every room needs people who can moderate it, and setting that by hand does not survive membership
changes any better than membership itself does. Deriving power levels from Authentik keeps the roster
of who-may-moderate in the same place as the rest of your access control, and keeps it current.

### What a user sees

- Usually nothing: you are an ordinary member with no special buttons.
- If you are in a group that grants an elevated level, or you are an Authentik superuser, your client
  shows you moderator or admin controls in the rooms Onbot manages.

### What an admin should know

- **A group attribute carries the level.** `authentik_group_attr_for_matrix_power_level` names an
  attribute holding an integer from 0 to 100; members of that group receive it as their power level.
  When a user's several groups disagree, the highest wins.
- **Superusers become admins** (`make_authentik_superusers_matrix_room_admin`, on by default):
  Authentik superusers are granted power level 100 in the rooms they are in, overriding the group
  attribute.
- **Levels are withdrawn as well as granted.** Lose the group that gave you a level and Onbot lowers
  you back to the room default; it only ever touches the users it manages, leaving hand-promoted
  accounts and its own account alone.
- **The bot always outranks the room.** As the room's creator it sits above everyone with an
  effectively infinite power level, so it can always manage membership and settings. One consequence
  worth knowing: a user manually raised to the maximum level in an older room cannot be lowered by
  anyone — nothing outranks the top — which is the trap behind the read-only welcome-room note in
  [troubleshooting.md](troubleshooting.md#welcome-rooms-created-before-onbot-made-them-read-only).

**Settings:** `authentik_group_attr_for_matrix_power_level` and
`make_authentik_superusers_matrix_room_admin` under
[`sync_matrix_rooms_based_on_authentik_groups`](CONFIG_REFERENCE.md). The room-creator rules are
explained in [ADR-0011](adr/0011-room-version-12.md).

---

## Parent space

**In one sentence:** all the group rooms are gathered under one Matrix space, so they appear as a
tidy, browsable group in the room list instead of scattered loose at the top of it.

### The problem it solves

A directory of any size produces a lot of rooms, and dozens of them floating unsorted in every user's
room list is noise. A space is a folder: it collects the managed rooms in one place, gives new users
somewhere to browse what exists, and is the mechanism that makes a [visitor lobby](#visitor-lobby)
joinable at all.

### What a user sees

- A space (for example *Company Chat*) that contains your group rooms. Every synced user is added to
  the space, so it is always there for you.
- From the space you can see and open the rooms you belong to.

### What an admin should know

- **It is on by default** (`create_matrix_rooms_in_a_matrix_space.enabled`). Disable it to leave the
  group rooms unparented at the top of the room list.
- **Onbot finds the space by its alias and creates it if it is missing**
  (`create_matrix_space_if_not_exists.enabled`). Turn creation off to build and curate the space
  yourself, in which case Onbot only adds rooms to it; if the space is then missing entirely, Onbot
  refuses to start rather than scatter rooms.
- **Its name, topic and icon are configurable.** The icon is downloaded and re-applied only when its
  source URL changes, so it also updates an already existing space.
- **Space membership is add-only** — Onbot never kicks anyone out of the space, only out of
  individual rooms.
- Onboarding notice-board rooms can optionally be gathered here too
  (`place_onboarding_rooms_in_space`, off by default).

**Settings:** all the fields under
[`create_matrix_rooms_in_a_matrix_space`](CONFIG_REFERENCE.md).

---

## Welcome messages

**In one sentence:** the first time Onbot sees a new user it opens a private 1:1 "notice board" room
with them and posts your configured getting-started messages, once.

### The problem it solves

A new person lands in a chat system they may never have used and needs orienting — who this bot is,
where the documentation lives, the warning to save their encryption security key. One small,
read-only room delivers that reliably to everyone, without an admin having to greet each arrival by
hand.

### What a user sees

- A room (by default named *Announcements*) that the bot opened and put you straight into — there is
  no invitation to accept.
- The bot posts a few welcome messages. You **cannot** reply: it is a read-only notice board, not a
  conversation. It is unencrypted, so your first impression is the messages themselves rather than a
  screen of "unable to decrypt".
- Later company-wide announcements arrive in this same room.

### What an admin should know

- **The messages are yours to write.** `welcome_new_users_messages` is the list, sent in order;
  `null` or an empty list disables the welcome room entirely. Each message is sent at most once per
  user and matched by its text, so editing one message re-sends *that* message to everyone.
- **The room's name and topic** come from `onboarding_room_name` and `onboarding_room_topic`, read
  only when the room is created. The room needs a name because the bot joins the user directly rather
  than through a direct-message invitation, and without one the client would show it as an untitled
  room.
- **Users are force-joined by default** (`force_join_onboarding_room`), so the notices land somewhere
  they actually see. The join happens exactly once — a user who leaves is not dragged back — and
  Onbot falls back to a plain invitation if the join is refused.
- **The welcome room is deliberately separate and unencrypted**, even when your group rooms are
  encrypted: the bot posts here, and it stays outside encryption everywhere it writes messages.
- **A gotcha for old deployments:** welcome rooms created by a much earlier Onbot granted the user the
  same power level as the bot, so they can still write in them and cannot be demoted. The fix is in
  [troubleshooting.md](troubleshooting.md#welcome-rooms-created-before-onbot-made-them-read-only).

**Settings:** `welcome_new_users_messages`, `onboarding_room_name`, `onboarding_room_topic` and
`force_join_onboarding_room` in the [configuration reference](CONFIG_REFERENCE.md). The design is in
[ADR-0003](adr/0003-onboarding-is-event-driven.md) and [ADR-0009](adr/0009-e2ee-stance.md).

---

## Room names, topics and icons

**In one sentence:** a room's name, topic and icon are derived from its Authentik group and, by
default, kept in step as the group changes.

### The problem it solves

Rooms should be recognisable and self-describing — and that description should not rot when a group is
renamed or its purpose rewritten. Taking the name, topic and icon from Authentik keeps them accurate
without anyone editing rooms by hand.

### What a user sees

- The room is named after its group, carries a topic that explains it, and may show an icon.
- If an admin renames or re-describes the group in Authentik, the room's name and topic follow.

### What an admin should know

- **Name and topic have a source and an optional prefix.** The name comes from
  `matrix_name_from_authentik_attribute` (the group name by default) with an optional `name_prefix`;
  the topic comes from `matrix_topic_from_authentik_attribute` with an optional `topic_prefix`, and an
  unset source simply leaves the topic blank.
- **The icon is a URL held on the group.** `room_avatar_url_attribute` names a key inside the group's
  custom attributes that holds an HTTP(S) image URL; Onbot downloads and re-uploads it, and only when
  the URL changes, so it also updates rooms that already exist. (The parent space has its own icon
  setting.)
- **Keeping in step is a switch.** `keep_updating_matrix_attributes_from_authentik` (on by default)
  re-applies the name and topic every reconcile, overwriting any drift. Turn it off to let room
  admins rename a room in their client and keep the change. Note this covers name and topic only — a
  room's alias is fixed when it is created and never changes.

**Settings:** the `matrix_name_from_authentik_attribute`, `matrix_topic_from_authentik_attribute`,
`*_prefix` and `keep_updating_matrix_attributes_from_authentik` fields under
[`matrix_room_default_settings`](CONFIG_REFERENCE.md), plus `room_avatar_url_attribute`.

---

## Offboarding

**In one sentence:** when someone is disabled or deleted in Authentik, Onbot winds their Matrix
access down safely — but by default it only records an audit trail of what it *would* do and touches
nothing.

### The problem it solves

A person who has left should not keep chat access, yet revoking it is irreversible and easy to get
wrong, and an accidental upstream disable must not erase a real account. Onbot automates the wind-down
while defaulting to safe: it watches, it records, and it does nothing destructive until you
deliberately opt in — and even then it waits out generous grace periods that a mistake can be undone
within.

### What a user sees

- Nothing, until it actually happens. After a grace period a disabled user is logged out of Matrix,
  and much later — a year, by default — their account is deactivated.
- If they are re-enabled in Authentik within the window, the pending action is cancelled and they
  carry on as if nothing happened.

### What an admin should know

- **Two switches, and the second is the important one.** `enabled` (on by default) turns *detection*
  on, but on its own that is audit-only. `dry_run` (on by default) is the real quarantine: while it
  is `true`, Onbot only writes what it *would* do to the `onbot.lifecycle.audit` log and revokes
  nothing. Set it to `false` to make offboarding act. Run with the default first and read the audit
  log before you flip it.
- **Grace periods absorb accidents.** `deactivate_after_n_sec` (24 hours by default) is how long a
  user stays disabled before being logged out; `delete_after_n_sec` (365 days by default, or `null`
  to never delete) before the account is erased. `include_user_media_on_delete` also removes their
  uploads, for data-protection erasure.
- **On a MAS-fronted homeserver you must configure `mas_admin`.** There the Matrix session belongs to
  the Matrix Authentication Service, and the Synapse admin API cannot revoke it — without `mas_admin`
  credentials the logout is a no-op against live sessions. This, and the `dry_run` default, are the
  two reasons "disabled users keep their access" in
  [troubleshooting.md](troubleshooting.md).
- **The destructive path is tightly scoped.** It can only ever touch accounts Onbot can positively
  map to a now-*disabled* Authentik user that already has a Matrix account; the bot and both ignore
  lists are always excluded, so unrelated admin and service accounts are structurally out of reach.

**Settings:** the fields under
[`deactivate_disabled_authentik_users_in_matrix`](CONFIG_REFERENCE.md) (inside
`sync_authentik_users_with_matrix_rooms`), and [`mas_admin`](CONFIG_REFERENCE.md). The safety design
is in [ADR-0005](adr/0005-quarantine-lifecycle.md).

---

## Admin control room and broadcasts

**In one sentence:** an optional Matrix room where allowlisted administrators type commands — most
importantly `!announce`, which sends a message into every user's notice board — with the same
capability available as an `onbot broadcast` command on the command line.

### The problem it solves

Sometimes you have to reach everyone at once: scheduled maintenance, an outage, a policy notice. Onbot
already owns a read-only notice board with every user, so it can fan one message out to all of them.
The control room is a way to trigger that from inside Matrix, for operators who would rather not drop
to a shell.

### What a user sees

- **As an ordinary user:** an announcement simply appears in your welcome / notice-board room.
- **As an administrator:** you are invited to the control room. Typing `!announce <message>`,
  `!status` or `!help` does something; any message *without* a `!` prefix is ignored, so admins can
  discuss an incident in the room without a stray sentence paging the whole company.

### What an admin should know

- **It is off by default** (`admin_room.enabled`), because it lets anyone on the allowlist message
  every user on the server — a capability to turn on deliberately.
- **Authorisation is an allowlist, never a room power level.** The allowlist is the union of
  `admin_user_ids` (full Matrix IDs listed by hand — the break-glass floor that keeps working even
  when Authentik is unreachable) and the members of the Authentik groups in
  `authentik_group_pks_granting_bot_admin`. An empty union means nobody may command the bot, even
  someone who is somehow in the room.
- **Authentik superusers are deliberately *not* bot admins**, and there is no option to make them so:
  a capability that pages everyone must not widen because somebody was granted an unrelated role
  upstream.
- **The set is live.** Removing somebody from the granting group revokes their commands within a
  refresh interval, without a restart; they keep their seat in the room and can still read it.
- **It is found by alias** (`admin_room.alias`), so changing the alias later makes Onbot create a
  second, empty room rather than rename the first. The room is unencrypted and unfederated, so the
  bot can read it and it cannot be commanded from off-server.
- **Broadcasts are fail-soft.** They go out as quiet `m.notice` messages, rate-limited across the
  fan-out, and one unreachable room is reported rather than allowed to silence the announcement for
  everyone else. The `onbot broadcast` CLI command does the same job without a room.

**Settings:** all the fields under [`admin_room`](CONFIG_REFERENCE.md). The rationale for admitting a
command-reading room at all is in [ADR-0010](adr/0010-admin-control-room.md).

---

## Cleaning up orphaned rooms

**In one sentence:** when a group that had a room disappears — deleted, or no longer opted into chat —
Onbot can close that room down, so no stale, unmanaged room is left behind.

### The problem it solves

If a group goes away, its room should not linger as an orphan that Onbot no longer governs but users
still sit in. Closing it keeps the Matrix side a faithful mirror of Authentik, with nothing left over
that nobody owns.

### What a user sees

- A room you were in becomes closed to new activity, and you are removed with a short message saying
  the group behind it is no longer synced from the central directory.
- If that group later comes back, the room is reopened and managed again.

### What an admin should know

- **It is off by default** (`disable_rooms_when_mapped_authentik_group_disappears`). When on, a room
  whose mapped group has vanished is blocked and every member is kicked with an explanatory reason.
- **"Disappears" is broader than "deleted."** A group that loses the attribute, name prefix or parent
  that opted it into chat counts as gone too, and its room is closed the same way.
- **Only Onbot's own rooms are ever touched.** A room is recognised as obsolete by an internal stamp
  Onbot writes recording which group it belongs to; rooms without that stamp — unrelated rooms, the
  onboarding notice boards — are structurally out of reach.
- **Deletion is a further, irreversible step.** `delete_disabled_rooms` (off by default) also deletes
  the room through the Synapse admin API instead of only blocking it, destroying its history for good.
  Leave it off unless you are sure.
- A group's [visitor lobby](#visitor-lobby) is torn down together with its group room, so a blocked
  room is never left beside a still-open lobby.

**Settings:** `disable_rooms_when_mapped_authentik_group_disappears` and `delete_disabled_rooms` under
[`sync_matrix_rooms_based_on_authentik_groups`](CONFIG_REFERENCE.md).
</content>
