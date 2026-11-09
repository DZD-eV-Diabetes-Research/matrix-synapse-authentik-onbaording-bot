# Onbot features

This page explains, in plain language, what Onbot can do for you. It is written for two readers:

- **Users**: people who log into the chat and end up in rooms. You do not configure anything; this
  tells you what the bot is doing on your behalf and why a room appeared.
- **Admins**: people who run Onbot. Each feature says what it is for, when to turn it on, and where
  to find the exact settings.

Every feature is optional unless noted, and each links to its settings in the
[configuration reference](CONFIG_REFERENCE.md). For the practical "what do I type into an Authentik
group to get a room with this topic, icon and lobby" version, see
[Configuring Authentik groups](authentik-groups.md). Onbot's guiding idea: **Authentik is the source of
truth, and Onbot mirrors it into Matrix**: groups become rooms, membership becomes room membership,
roles become power levels. You manage people in Authentik; the chat follows.

## Feature overview

- **[Visitor lobby](#visitor-lobby)**: an open "front door" room beside a private group room, that
  anyone in the wider space can find and join.
- **[Group rooms](#group-rooms)**: each Authentik group becomes a Matrix room.
- **[Membership sync](#membership-sync)**: group membership becomes room membership, added and
  removed automatically.
- **[Power levels from Authentik](#power-levels-from-authentik)**: roles and group attributes decide
  who is a room moderator or admin.
- **[Parent space](#parent-space)**: all the group rooms are gathered under one space in the user's
  room list.
- **[Welcome messages](#welcome-messages)**: a new user gets a friendly 1:1 notice board with
  getting-started messages.
- **[Room names, topics and icons](#room-names-topics-and-icons)**: kept in step with Authentik.
- **[Offboarding](#offboarding)**: when someone is disabled in Authentik, their chat access is wound
  down safely, with a dry-run/audit default.
- **[Admin control room and broadcasts](#admin-control-room-and-broadcasts)**: announce a message to
  everyone, from a room or the command line.
- **[Cleaning up orphaned rooms](#cleaning-up-orphaned-rooms)**: when a group disappears, its room is
  closed.
- **[Guard against mass removals](#guard-against-mass-removals)**: when a pass would remove far more
  than usual, the removals wait for an administrator's confirmation.

---

## Visitor lobby

**In one sentence:** the private group room stays exactly as private as it is today, and beside it
Onbot can keep a second, open room, its *lobby*, that anyone in the wider space can find and walk
into, and that nobody is ever kicked out of.

### The problem it solves

A group room like *Düsseldorf* is private: only members of the Authentik *Düsseldorf* group are in
it, and outsiders cannot see it. That is usually what you want, but sometimes you want the group to
be **reachable**. Someone from another team should be able to find *Düsseldorf*, drop in, and ask a
question, without being made a full member of the group.

You cannot simply "open up" the existing room. A Matrix room remembers its history, and by default a
newcomer who joins can scroll all the way back to the day it was created, through months of
conversations nobody held with visitors in mind. Opening a live room exposes its past.

A lobby avoids that completely, because **it is a brand-new, empty room**. There is no history to
leak. The group's real working room is left untouched; the lobby sits next to it as an open door.

### What a user sees

- **If you are in the group** (e.g. you are in Authentik's *Düsseldorf* group): you are in **both**
  rooms: *Düsseldorf* (your private working room) and *Düsseldorf (Lobby)* (where visitors may turn
  up). Onbot puts you in the lobby automatically so a visitor who walks in finds somebody there.
- **If you are not in the group** but you are in the shared space: in the space's room list you see
  **the lobby, and only the lobby**, not the private group room. You can join the lobby yourself,
  with one click, no invitation needed. You **cannot** see or join the private group room.
- **Nobody is ever kicked from a lobby.** Once you are in, you stay, even if you were never in the
  group, and even if you later leave the group. (The one exception is if your whole account is
  deactivated, which removes you from every room.)

The lobby is named after the group with a suffix, so it is obvious which room is which:
*Düsseldorf* is the working room, *Düsseldorf (Lobby)* is the front door. Its topic spells out the
arrangement, so a visitor is never confused about where they have landed, and it wears the same
[icon](#room-names-topics-and-icons) as the group room, so the pair reads as one place in the room list.

### What an admin should know

- **It is off by default.** A lobby is a deliberate choice, not something that appears on upgrade.
- **Turn it on per deployment or per group.** Set `visitor_lobby_enabled` on the default room
  settings to give every group a lobby, or let a group owner opt in from Authentik by setting the
  group attribute named in `matrix_room_visitor_lobby_from_authentik_attribute`; no config redeploy
  needed.
- **A lobby needs the parent space.** The lobby is joinable precisely by members of the space, so the
  space feature (`create_matrix_rooms_in_a_matrix_space`) must be on. Onbot refuses to start if you
  enable a lobby without a space, rather than quietly creating a room nobody can join.
- **Lobbies are unencrypted by default**, on purpose, and this is *weaker* than the group room's
  default. A room open to the whole space gains little from encryption, while encryption would greet
  every visitor with a screen of "unable to decrypt". You can turn it on
  (`visitor_lobby_end2end_encryption_enabled`), but only before the lobby is first created:
  encryption can never be switched off again.
- **The group's members are seeded into the lobby** by default, so it is not a ghost town. If you run
  a large group and would rather staff an empty lobby deliberately, turn
  `visitor_lobby_inject_group_members` off.
- **Switching a lobby off closes it.** When a group no longer wants a lobby (the group attribute set
  to `false`, or `visitor_lobby_enabled` turned off), Onbot sets the lobby's join rule to invite-only
  and takes it out of the space. Nobody is kicked; the people in it can stay and talk. The power
  levels Onbot granted there (the global room admins) are withdrawn, unless user sync is switched off
  (then Onbot leaves power levels alone everywhere). Closing and each withdrawal count
  as removals for the [guard](#guard-against-mass-removals), so many lobbies closing at once wait for
  a confirmation. Turn the lobby on again and Onbot reopens it, puts it back into the space and seats
  the global room admins again. A lobby a server admin blocked is a hold: Onbot neither closes it nor
  withdraws levels there while the block exists, reports it once, and closes it once the block is
  lifted.
- **A lobby somebody closed by hand stays closed.** If a room admin of the lobby changes its join rule
  to invite-only (or knock, or private), Onbot leaves it that way, logs a warning and reports it once
  in the admin control room. To hand the lobby back to Onbot, an admin types
  `!lobby reopen <group>` there (the group's Authentik pk; the lobby's room id or alias work too):
  Onbot opens the lobby to the space again and manages it from the configuration from then on.
  Onbot tells its own changes from a person's by who sent the join rule, so this survives restarts.
  A lobby somebody opened further (to `public`) is set back to the space-only rule.
- **One honest limitation.** The lobby hides the group room's *contents*, not the bare fact that it
  exists. A determined person can learn that a room with that ID exists (though they still cannot
  join it, read it, or see its members). If even the existence must be secret, the answer is a second
  space, not a lobby; see [troubleshooting.md](troubleshooting.md#the-lobby-room-id-leak-and-why-a-lobby-is-a-second-room-not-a-visitable-first-one).

**Settings:** all the `visitor_lobby_*` fields under
[`matrix_room_default_settings`](CONFIG_REFERENCE.md), plus
`matrix_room_visitor_lobby_from_authentik_attribute`. The full design rationale is in
[ADR-0012](adr/0012-lobby-rooms.md).

---

## Group rooms

**In one sentence:** every Authentik group Onbot is told to mirror becomes one Matrix room, created
once and then kept as the place that group meets.

### The problem it solves

You already decide who belongs together in Authentik: a team, a site, a project is a group there.
Building the same set of rooms by hand in Matrix, and keeping it current as groups are created and
retired, is exactly the busywork Onbot removes: you manage the group, and the room follows. Because
not every group is meant to be a chat room, Onbot only turns a group into a room when the group opts
in, so your identity directory and your chat rooms need not be the same list.

### What a user sees

- A room appears in your list for each mirrored group you belong to. You did not create it or ask to
  join; it is simply there because you are in the group.
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
  outright, before every other filter. Group pks can be written with or without hyphens, in any case.
- **A room is created once and then reused.** By default its permanent Matrix alias is derived from
  the group's stable primary key, not its name (`matrix_alias_from_authentik_attribute`), precisely
  so that renaming the group in Authentik does not strand the room, because a Matrix alias can never
  be changed after the room exists.
- **Onbot recognises its rooms by who created them, not by their alias.** A room belongs to a group
  only when Onbot itself created it and it carries Onbot's marker naming that group. The marker is
  part of the room from its creation, and only Onbot can change it (a room moderator cannot), so
  nobody can move a room to another group. Onbot also checks who wrote the marker: one written by
  anybody but Onbot (possible in rooms from older versions, or for somebody at Onbot's own level in a
  room older than version 12) is reported, and the room is neither adopted nor torn down. The alias
  is only the room's address: a moderator who changes or removes it changes nothing for Onbot, and
  Onbot does not build a second room. A removed alias is put back when it is still free, and so is an
  empty main address; when another room took the alias meanwhile, Onbot reports it once and leaves
  it there.
- **A room somebody else created at a group's alias is never taken over.** Any user can claim a free
  alias, so a user could create a room at the alias a group is about to get. Onbot then skips that
  group, reports it in the log and the admin control room, and creates nothing: it neither joins the
  group into that room nor builds a second one beside it. Remove the alias from that room (a server
  admin can) and the group gets its room on the next pass. The same applies when two groups would get
  the same alias (for example `team-a` and `teama` after the dashes are stripped): neither gets a new
  room until the attributes differ.
- **Changing the alias settings later keeps the existing rooms.** A group keeps its room when
  `alias_prefix` or the alias attribute changes; the room keeps its old alias as its main address and
  also gets the new one, if that is free. (Before this version a changed alias stranded the old room
  and built a new, empty one.)
- **Rooms are repaired, not only created.** On every pass Onbot checks, reading first and writing only
  on a difference, that each of its rooms is still linked into the space (and, for a lobby, suggested
  there), still carries its marker as Onbot wrote it, and that the marker is still protected. A room
  whose creation was cut off halfway, a room from before the space existed, or the rooms of a space
  that was recreated are linked in again (the links Onbot wrote to the old space are removed, also on
  a later pass when the first attempt fails, as long as Onbot is not restarted in between; after a
  restart a leftover link to the old space stays until a human removes it. A link a human added, to
  another space or into the old one, stays). A marker somebody at Onbot's own level removed or rewrote,
  or whose protection they lowered, is restored from what Onbot last wrote there itself, and
  reported once; Onbot never takes over what such a marker claims. Each repair stands on its own: one
  the server refuses is reported, and the others still run. This works while Onbot runs: a marker
  removed or rewritten while Onbot was stopped (or before its last restart) makes the room look like
  one that never belonged to the group, and Onbot does not take it back on its own. It reports the
  group instead. An allowlisted admin hands such a group room back with `!room adopt <group pk>
  <room id or alias>` in the admin control room, and a visitor lobby with `!room adopt lobby
  <group pk> <room id or alias>`: Onbot checks that it created the room, that the group is one it
  keeps a room (or lobby) for and has no other one, that the room is not a space or a user's notice
  board, writes and protects the marker, and manages the room from the next pass on. See
  [A room lost its onbot marker](troubleshooting.md#a-room-lost-its-onbot-marker).
- **A room a server admin blocked stays blocked.** Synapse records who blocked a room. Onbot unblocks
  only rooms it blocked itself (when their group disappeared and came back). A group room or lobby a
  server admin blocked (for moderation, a legal hold) is left blocked, and Onbot does not manage it
  while it is: no joins, kicks, levels or renames. It is reported once in the admin control room.
  When Synapse cannot say whether a room is blocked, Onbot leaves the room alone for that pass too.
- **Rooms upgraded by a human are left alone.** When somebody other than Onbot upgrades a group room to
  a new room version (only possible when `chatroom_params` lowered the upgrade permission), Onbot
  reports it and skips that group until an admin decides; it does not follow the upgrade.
- **Upgrading from a version before room identity:** existing rooms carry Onbot's marker but not
  its protection. On the first pass Onbot adopts each one once, through its alias: a marked room that
  Onbot created and that sits at the alias the current configuration gives it is protected from then
  on and found by its marker alone. Marked rooms that are not at their alias (for example stranded by
  an earlier alias change) are reported and left alone, and so are rooms Onbot did not create. If your
  rooms were created by a different bot account than the one configured now, they are not adopted:
  every such group is reported as "belongs to room ..., which the bot did not create".
- **Rooms are end-to-end encrypted by default** (`end2end_encryption_enabled`). Like all encryption
  in Matrix this can never be turned off again once a room has it, so decide before the room is
  created. You can override this and other settings for a single group through
  `per_authentik_group_pk_matrix_room_settings`.

**Settings:** [`sync_matrix_rooms_based_on_authentik_groups`](CONFIG_REFERENCE.md) chooses which
groups become rooms; [`matrix_room_default_settings`](CONFIG_REFERENCE.md) shapes each room. For the
Authentik side of this, every attribute you can set on a group and what it does, see
[Configuring Authentik groups](authentik-groups.md).

---

## Membership sync

**In one sentence:** whoever is in the Authentik group is put into its room, and (by default)
whoever leaves the group is removed from its room, so room membership stays a faithful mirror of the
group.

### The problem it solves

A room that stands for a group is only useful if being in the group actually puts you there and
leaving actually takes you out. Kept by hand, that mirror drifts the moment somebody forgets an
invite or a kick. Onbot makes Authentik group membership the single lever: pull it, and the room
follows.

### What a user sees

- Join a group in Authentik and you appear in its room, with no invitation to accept: you are simply
  a member.
- Leave the group and you are removed from the room (unless your admin has turned removals off).
- You are never asked to manage any of this.

### What an admin should know

- **Who is considered at all** is narrowed by the user filters: `sync_only_users_in_authentik_pathes`,
  `sync_only_users_with_authentik_attributes`, and `sync_only_users_of_groups_with_id`. Only *active*
  Authentik users are synced, and `authentik_user_ignore_list` keeps named accounts (service,
  break-glass) out of chat entirely. Group membership is checked on each user Authentik returns (its
  own `groups`), not only through Authentik's group filter: a user who is in none of the configured
  groups is left out and logged as an error. Group pks can be written with or without hyphens. If
  Authentik ever stopped naming the groups in its user objects, the scope would be empty; the
  [guard](#guard-against-mass-removals) holds that like any empty answer, so nobody is removed or
  offboarded until an admin looks.
- **The identity match is the thing to get right.** Onbot maps each Authentik user to a Matrix ID
  using `authentik_username_mapping_attribute`. On a homeserver fronted by the Matrix Authentication
  Service this *must* agree with the localpart the login flow produces, or the computed Matrix IDs
  name accounts that do not exist and nobody is ever added. If users never land in their rooms, this
  is almost always why; see the first row of [troubleshooting.md](troubleshooting.md) and the
  [configuration guide](configuration.md#the-mxid-localpart-contract). With `mas_admin` configured,
  Onbot asks MAS which account each user owns, and it records every user's account the first time it
  sees it: a user renamed in Authentik keeps their account, rooms and power levels, and two
  Authentik users claiming one account are skipped and reported (unless MAS links it to one of them)
  ([details](configuration.md#renamed-users-keep-their-account)).
- **Removals are gated by a switch.**
  `kick_matrix_room_members_not_in_mapped_authentik_group_anymore` is on by default; turn it off to
  make Onbot *add-only*, so it never kicks anyone and users keep rooms after losing the group that
  granted them.
- **Switching user sync off stops membership management.** With
  `sync_authentik_users_with_matrix_rooms.enabled: false` Onbot adds nobody, removes nobody and leaves
  power levels alone; it only keeps the rooms themselves (name, topic, lobby join rule) in step.
- **An implausible answer never empties the rooms.** Removals go through the
  [guard against mass removals](#guard-against-mass-removals): when Authentik suddenly returns far
  fewer users or groups than before, or a pass would remove more than a handful of members at once,
  the removals wait for an administrator's `!confirm` in the admin control room. Additions still
  happen.
- **Matrix-only accounts are protected.** `matrix_user_ignore_list` shields server admins, other
  bots and bridge users from a sync that would otherwise see them as unknown to Authentik: they are
  never removed from a group room. The bot's own account is always protected.
- **One broken room does not hold up the others.** A member the server refuses to add (for example
  somebody banned from the room), a member Onbot may not remove, or a room that refuses a change is
  skipped, and everything else, other rooms and the offboarding of disabled accounts included, still
  runs. Each such room is logged once per pass as a warning that starts with
  `reconcile: room not converged:` and names the room, the Authentik group and what went wrong.
- Membership is reconciled on a schedule *and* whenever Authentik changes, so it also quietly repairs
  a member somebody kicked by hand inside Matrix.

**Settings:** all the fields under
[`sync_authentik_users_with_matrix_rooms`](CONFIG_REFERENCE.md).

---

## Power levels from Authentik

**In one sentence:** an Authentik group can carry an attribute that sets its members' power level
(their moderator or admin rank) in the room, and Authentik superusers become room admins
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
- **Levels are withdrawn as well as granted, but only the ones Onbot set.** Onbot records every level
  it writes, in its own per-room account data, which no room member can see or edit. When you stop
  qualifying for a level, for whatever reason (you left the group, lost the power-level group or
  superuser status, your account was disabled or left the sync scope), that entry is removed and you
  drop back to the room default. Levels a human set in a Matrix client are never withdrawn, and
  neither is a level Onbot set that a human changed afterwards: from then on it is theirs. Onbot's
  own account is never written.
- **Upgrading from a version without the record:** on its first pass Onbot adopts every existing
  level that matches what it would grant anyway. A user who had already left their group before the
  upgrade keeps their old level until somebody removes it by hand.
- **In rooms Onbot created as room version 12, the bot outranks everyone.** As the room's creator it
  holds an effectively infinite power level, so it can always manage membership and settings.
- **In older rooms the bot is an ordinary member at 100.** That is the case for rooms before room
  version 12: rooms adopted from a legacy bot, or rooms created while `synapse_server.room_version`
  pins an older version. Matrix forbids lowering or removing anybody at or above your own level, so
  there Onbot never grants more than 99 (superusers and global room admins included), and it does
  not try to change or remove users who already sit at 100. It reports them instead, once per pass,
  in the `reconcile: room not converged:` warning. A room admin has to lower them by hand (they can
  lower themselves; nobody else can). The same trap is behind the read-only welcome-room note in
  [troubleshooting.md](troubleshooting.md#welcome-rooms-created-before-onbot-made-them-read-only).
- **Extra room creators are out of reach.** A creator added through `chatroom_params`
  (`additional_creators`) has an infinite power level that nobody can lower or remove. Onbot skips
  such a user and reports the room.

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
- **Onbot finds the space it created for this configuration and creates it if it is missing**
  (`create_matrix_space_if_not_exists.enabled`). The space remembers the `alias` it was created for,
  so a changed `alias` setting makes Onbot create a new space. If a room it did not create already
  holds the alias, Onbot does not use it and the room stage stops with an error instead. Turn creation off to build and curate the space
  yourself, in which case Onbot only adds rooms to it; if the space is then missing entirely, Onbot
  refuses to start rather than scatter rooms.
- **Its name, topic and icon are configurable.** The icon is downloaded at startup, looked at again
  about once a day, and re-applied only when its source URL or the picture behind it changed, so it
  also updates an already existing space.
- **Space membership is add-only**: Onbot never kicks anyone out of the space, only out of
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

A new person lands in a chat system they may never have used and needs orienting: who this bot is,
where the documentation lives, the warning to save their encryption security key. One small,
read-only room delivers that reliably to everyone, without an admin having to greet each arrival by
hand.

### What a user sees

- A room (by default named *Announcements*) that the bot opened and put you straight into; there is
  no invitation to accept.
- The bot posts a few welcome messages. You **cannot** reply: it is a read-only notice board, not a
  conversation. It is unencrypted, so your first impression is the messages themselves rather than a
  screen of "unable to decrypt".
- Later company-wide announcements arrive in this same room.

### What an admin should know

- **Only mapped users get a notice board.** A user is welcomed once a reconcile pass has mapped them:
  an active Authentik user in the sync scope with a Matrix account, not ignored and not part of an
  identity conflict. Somebody who merely joins a room the bot is in (a guest from another homeserver
  invited into a group room, a local account that is not in Authentik, a break-glass admin entering
  the control room) gets nothing, and neither do broadcasts reach them. With
  `sync_authentik_users_with_matrix_rooms.enabled: false` nobody is mapped, so nobody is welcomed.
- **Welcomes do not slow down the pass.** The pass hands the mapped users to a small queue and goes
  on converging rooms; a few welcomes are delivered at a time beside it. A user who joins a room is
  welcomed right away if the last pass mapped them, otherwise with the pass that maps them.
- **The messages are yours to write.** `welcome_new_users_messages` is the list, sent in order;
  `null` or an empty list disables the welcome room entirely. Without notice boards, `!announce` and
  `onbot broadcast` have no room to post into, so a deployment that wants announcements but no
  welcome text needs at least one short message. Each message is sent at most once per
  user. A plain entry is matched by its text, so editing it re-sends *that* message to everyone
  (whitespace at line ends and blank lines around the text do not count). To fix a typo without
  re-sending, give the message an id, see [message ids](#message-ids) below.
- **Messages are Markdown, and can link to your space.** See
  [writing welcome messages](#writing-welcome-messages) below.
- **The room's name and topic** come from `onboarding_room_name` and `onboarding_room_topic`, read
  only when the room is created. The room needs a name because the bot joins the user directly rather
  than through a direct-message invitation, and without one the client would show it as an untitled
  room.
- **Users are force-joined by default** (`force_join_onboarding_room`), so the notices land somewhere
  they actually see. The join happens exactly once, so a user who leaves is not dragged back, and
  Onbot falls back to a plain invitation if the join is refused.
- **A user who leaves gets one invitation back.** The room is invite-only, so without it a single
  click on "Leave" would cut them off from every later announcement. Onbot notices the leave and
  invites them once; whether they accept is up to them, and a user who declines or leaves again is
  not invited a second time. Broadcasts skip rooms whose user is no longer in them. An admin can
  always put a user back with Synapse's admin join API.
- **A notice board the bot is no longer in is replaced.** If such a room was purged by hand, or the
  user of a very old room removed the bot, Onbot drops it from its `m.direct` and opens a new notice
  board (which sends the welcome messages again). Onbot goes by its own membership in the room, so a
  kick is noticed too (Synapse keeps answering the reads of a member that was kicked). It looks when
  it next checks that user: after a restart, or when the user leaves a room. A notice board whose onbot state event was edited
  into something unreadable is reported in the log once per start and left alone.
- **The welcome room is deliberately separate and unencrypted**, even when your group rooms are
  encrypted: the bot posts here, and it stays outside encryption everywhere it writes messages.
- **A gotcha for old deployments:** welcome rooms created by a much earlier Onbot granted the user the
  same power level as the bot, so they can still write in them and cannot be demoted. The fix is in
  [troubleshooting.md](troubleshooting.md#welcome-rooms-created-before-onbot-made-them-read-only).

### Writing welcome messages

Each entry of `welcome_new_users_messages` is **Markdown** (CommonMark), so links, emphasis, lists,
headings and quotes all arrive formatted:

```yaml
welcome_new_users_messages:
  - |
    ## Welcome aboard, {user_id}

    I am the company bot. I will invite you to the rooms of the groups you are assigned to.

    - New to Matrix? Read the [Element user guide](https://element.io/user-guide).
    - Something broken? Write to [an admin](https://matrix.to/#/@admin:matrix.company.org).
  - |
    Everything open to everyone lives in **[{space_name}]({space_link})**.
    Open it to see which rooms you can join.
```

Matrix itself has no "this is Markdown" flag: the bot renders each message to HTML before sending it,
the same thing Element's composer does when you type. The Markdown source stays in the message as the
fallback for clients that show no formatting, and HTML written directly into a message is *not* passed
through, it appears as literal text.

Placeholders are filled in as the message is sent:

| Placeholder | Becomes |
|---|---|
| `{space_link}` | a `matrix.to` link to the parent space, e.g. `https://matrix.to/#/%23myspace:matrix.company.org` |
| `{space_alias}` | the space's alias, e.g. `#myspace:matrix.company.org` |
| `{space_name}` | the space's display name |
| `{user_id}` | the recipient's Matrix ID |

The link is built from the space's alias rather than its room id, so it needs no `?via=` server hints
and stays readable if a client ever shows it as plain text. The three `space_*` placeholders need
`create_matrix_rooms_in_a_matrix_space` enabled; a message using one while it is disabled is held back
with a warning in the log rather than sent with a gap in it, and goes out on the next reconcile once
you enable the space. Any other `{...}` in a message is left exactly as you wrote it.

Placeholders do not affect the once-per-user bookkeeping: a message is identified by the text in your
config, not by what an individual user saw, so renaming the space does not re-send anything.

#### Message ids

A plain entry is identified by its text, so correcting it sends the corrected message to every user
who already got it. An entry with an `id` is identified by the id instead:

```yaml
welcome_new_users_messages:
  - id: intro
    text: |
      Welcome aboard, {user_id}. I will invite you to the rooms of your groups.
  - Everything open to everyone lives in **[{space_name}]({space_link})**.
```

- Editing the `text` of an entry with an id sends nothing again. Changing the `id` sends the message
  once more to every onboarded user, which is the way to push a substantially new version.
- Giving an existing plain entry an id (with its text unchanged) sends nothing again either.
- Ids must be unique within the list.

Each send also carries a transaction id derived from the room and the message, so a send the bot
repeats because its answer was lost, or because the process died before it recorded the message, is
dropped by Synapse instead of posted twice (within the day Synapse remembers transaction ids).

**Settings:** `welcome_new_users_messages`, `onboarding_room_name`, `onboarding_room_topic` and
`force_join_onboarding_room` in the [configuration reference](CONFIG_REFERENCE.md). The design is in
[ADR-0003](adr/0003-onboarding-is-event-driven.md) and [ADR-0009](adr/0009-e2ee-stance.md).

---

## Room names, topics and icons

**In one sentence:** a room's name, topic and icon are derived from its Authentik group and, by
default, kept in step as the group changes.

### The problem it solves

Rooms should be recognisable and self-describing, and that description should not rot when a group is
renamed or its purpose rewritten. Taking the name, topic and icon from Authentik keeps them accurate
without anyone editing rooms by hand.

### What a user sees

- The room is named after its group, carries a topic that explains it, and may show an icon.
- If an admin renames or re-describes the group in Authentik, the room's name and topic follow.

### What an admin should know

- **Name and topic have a source and an optional prefix.** The name comes from
  `matrix_name_from_authentik_attribute` (the group's `chatroom_name` attribute by default, falling
  back to the group's own name when that attribute is absent) with an optional `name_prefix`; the
  topic comes from `matrix_topic_from_authentik_attribute` with an optional `topic_prefix`, and an
  unset source simply leaves the topic blank.
- **The icon is a URL held on the group.** `room_avatar_url_attribute` names a key inside the group's
  custom attributes that holds an HTTP(S) image URL; Onbot downloads it, looks at it again about once
  a day, and re-uploads it only when the URL or the picture behind it changed (it records a hash of
  the image), so a new logo at the same address reaches rooms that already exist within a day, without
  a restart. (The parent space
  has its own icon setting.) A group's [visitor lobby](#visitor-lobby) wears the same icon as its group room, from the
  same attribute: there is nothing extra to configure.
- **Keeping in step is a switch.** `keep_updating_matrix_attributes_from_authentik` (on by default)
  re-applies the name and topic every reconcile, to the group room and its lobby alike, overwriting
  any drift. Turn it off to let room admins rename a room in their client and keep the change, for
  every room in `matrix_room_default_settings` or for single groups in
  `per_authentik_group_pk_matrix_room_settings`. Note this covers name and topic only, a room's alias
  is fixed when it is created and never changes.

**Settings:** the `matrix_name_from_authentik_attribute`, `matrix_topic_from_authentik_attribute`,
`*_prefix` and `keep_updating_matrix_attributes_from_authentik` fields under
[`matrix_room_default_settings`](CONFIG_REFERENCE.md), plus `room_avatar_url_attribute`.

---

## Offboarding

**In one sentence:** when someone is disabled or deleted in Authentik, Onbot winds their Matrix
access down safely, but by default it only records an audit trail of what it *would* do and touches
nothing.

### The problem it solves

A person who has left should not keep chat access, yet revoking it is irreversible and easy to get
wrong, and an accidental upstream disable must not erase a real account. Onbot automates the wind-down
while defaulting to safe: it watches, it records, and it does nothing destructive until you
deliberately opt in, and even then it waits out generous grace periods that a mistake can be undone
within.

### What a user sees

- Nothing, until it actually happens. After a grace period a disabled or deleted user is locked out
  of Matrix, and much later (a year, by default) their account is deactivated.
- If they are re-enabled in Authentik before the account is deactivated, the pending action is
  cancelled or the lock is lifted, and they carry on after logging in again: the lock ends every
  session they had, so no old session (for example on a lost laptop) comes back. This needs a MAS
  whose admin API can end sessions; on an older MAS (0.14, for example) the lock only suspends
  sessions and lifting it brings the same sessions back. The startup log says which applies.
- A lock or unlock reaches a client that is already logged in only after about two minutes, because
  Synapse caches the answer of MAS for that long. The audit line is written at once.

### What an admin should know

- **Two switches, and the second is the important one.** `enabled` (on by default) turns *detection*
  on, but on its own that is audit-only. `dry_run` (on by default) is the real quarantine: while it
  is `true`, Onbot only writes what it *would* do to the `onbot.lifecycle.audit` log and changes
  nothing. Set it to `false` to make offboarding act. Run with the default first and read the audit
  log before you flip it.
- **Grace periods absorb accidents.** `lock_after_n_sec` (24 hours by default) is how long a
  user stays disabled before being locked out: under MAS the user is locked and their sessions are
  ended, which a re-enable undoes. `remove_after_n_sec`
  (365 days by default, or `null` to never erase) counts from the lock, so the account is erased
  (deactivated for good) `lock_after_n_sec + remove_after_n_sec` after the user was first seen
  disabled or deleted.
- **What erasure removes is up to you.** `gdpr_erase_on_delete` (off by default) also GDPR-erases the
  account: the display name and avatar go, and the user's messages are hidden from people who join a
  room later. `include_user_media_on_delete` (off by default) also deletes their uploads; many uploads
  are deleted over several passes until none is left. Both apply the same way with and without
  `mas_admin`. An older MAS (0.14, for example) always GDPR-erases on deactivation; with such a MAS
  and `gdpr_erase_on_delete: false` Onbot refuses the erasure (`FAILED ... refusing to deactivate`)
  rather than remove more than you configured. Upgrade MAS or turn the option on.
- **On a MAS-fronted homeserver you must configure `mas_admin`.** There the Matrix session belongs to
  the Matrix Authentication Service, and the Synapse admin API cannot end it: without `mas_admin`
  credentials a lock would be a no-op against live sessions. So Onbot asks the homeserver whether it
  delegates logins to MAS, and if it does while `mas_admin` is missing, it refuses to lock or erase
  anybody: it logs an error (`offboarding: the homeserver delegates logins ...`), reports it once in
  the admin room, and writes one `REFUSED` audit line per account that would have been locked or
  erased. Disabled users are still marked, so once `mas_admin` is configured the pending locks follow
  on the next pass. Onboarding and rooms are not affected. Until the homeserver has answered the
  question, nobody is locked either. This, and the `dry_run` default, are the two reasons "disabled
  users keep their access" in [troubleshooting.md](troubleshooting.md).
- **The startup log states the setup.** One `offboarding:` line lists `enabled`, `dry_run`, which
  effectors are in use, the grace periods and the erase options, and with `mas_admin` a second line
  lists what that MAS can do (end sessions, deactivate without GDPR erasure).
- **Who is offboarded.** A user who is disabled in Authentik while inside the sync scope (the same
  path, attribute and group filters that decide who is synced), and a *known* user who is deleted
  from Authentik. Onbot records every user it sees active, in scope and with a Matrix account
  (`authentik pk -> Matrix ID`, in the bot's account data), so a later deletion is noticed and goes
  through the same grace periods. A known user who is disabled is offboarded even when they also left
  the sync scope (moved to another path, removed from the synced group, opt-in attribute removed),
  so the usual "disable, archive and strip the groups" flow works in one step. A user deleted before
  Onbot ever saw them active is not known and not offboarded. Matrix accounts that no Authentik user ever mapped to (service, bridge or MAS-only
  accounts) are never touched, and neither is any account an active Authentik user maps to, in or
  out of scope.
- **Access only comes back on positive evidence.** Onbot lifts a lock only when the Authentik user it
  belongs to is present, active and inside the sync scope again, and only a lock Onbot set itself. A
  deletion, a removed attribute, a mapping change or an empty answer from Authentik never unlocks
  anybody. If Authentik suddenly returns no users at all, the offboarding step is skipped for that
  pass (`lifecycle step aborted` in the log).
- **Locks and erasures go through the guard.** A token that suddenly sees only some users makes
  everyone else look deleted; the [guard against mass removals](#guard-against-mass-removals)
  catches that drop and pauses every lock and erasure until an administrator confirms. It also
  limits how many accounts one pass may lock, which matters most when you switch `dry_run` off after
  a long dry run: everybody marked in the meantime would otherwise be locked in the first live pass.
  Erasures are irreversible, so from three in one pass on they wait for a confirmation as well.
  To see that pass before it happens, set `dry_run: false` and run `onbot reconcile-once --dry-run`
  first: it lists the locks and what the guard would pause, and changes nothing.
- **Leaving the scope freezes.** A user who is being offboarded (marked or locked) but leaves the sync
  scope while active (or was never known to Onbot as active), lands on an ignore list, or whose
  Matrix ID another active Authentik user now maps to, is *frozen*: no unlock and no further step. Onbot writes one `FROZEN` line to the audit log and posts
  it in the admin control room (when it is enabled). An admin decides by hand: bring the user
  back into scope (active means the lock is lifted, disabled means offboarding continues), or lock
  or deactivate the account in MAS directly. A user frozen because of `authentik_user_ignore_list`
  stays frozen when they are later deleted from Authentik; removing the name from the ignore list
  lets offboarding continue.
- **The bookkeeping heals and stays small.** An entry in Onbot's ledger that cannot be read (for
  example after a hand edit) is moved aside verbatim (`QUARANTINED` in the audit log) and that
  account is left alone until an admin repairs or removes it; everybody else is processed normally. A
  ledger that cannot be read at all, or was written by a newer Onbot, stops the offboarding step
  without overwriting anything. If the ledger is missing while MAS has locked users (the bot's
  account data was lost), Onbot logs one `ALERT`: it no longer knows which locks it set and will not
  lift them on a re-enable. A locked user who was deleted from Authentik while erasure is off
  (`remove_after_n_sec: null`) is forgotten after 90 days; the account stays locked.
- **A failing user does not hold up the rest.** If a step fails for one user (for example MAS does
  not know the username), the audit log says `FAILED`, the state does not advance, and Onbot retries
  that user with a growing delay (up to 6 hours) while everybody else is processed normally. Erasure
  records the account deactivation and the media deletion separately, so a retry resumes and never
  repeats the deactivation.
- **Dry-run touches nothing**, not even to unlock: every action, including lifting a lock, is only
  written to the audit log while `dry_run` is `true`. Each `DRY-RUN: would ...` line is written once
  when a user reaches that step, not again on every pass (again after a restart).

**Settings:** the fields under
[`deactivate_disabled_authentik_users_in_matrix`](CONFIG_REFERENCE.md) (inside
`sync_authentik_users_with_matrix_rooms`), and [`mas_admin`](CONFIG_REFERENCE.md). The safety design
is in [ADR-0005](adr/0005-quarantine-lifecycle.md).

---

## Admin control room and broadcasts

**In one sentence:** an optional Matrix room where allowlisted administrators type commands (most
importantly `!announce`, which sends a message into every user's notice board), with the same
capability available as an `onbot broadcast` command on the command line.

### The problem it solves

Sometimes you have to reach everyone at once: scheduled maintenance, an outage, a policy notice. Onbot
already owns a read-only notice board with every user, so it can fan one message out to all of them.
The control room is a way to trigger that from inside Matrix, for operators who would rather not drop
to a shell.

### What a user sees

- **As an ordinary user:** an announcement simply appears in your welcome / notice-board room.
- **As an administrator:** you are invited to the control room. Typing `!announce <message>`,
  `!status`, `!pending`, `!confirm <id>`, `!reject <id>`, `!guard reset`, `!lobby reopen <group>`,
  `!room adopt [lobby] <group> <room>` or `!help` does something;
  any message *without* a `!` prefix is ignored, so admins can discuss an incident in the room
  without a stray sentence paging the whole company.
- The bot also reports into the room: destructive actions it paused (see
  [guard against mass removals](#guard-against-mass-removals)), offboarding that needs a decision
  (a frozen user, a failing step), rooms it could not fully bring in line, and rooms a person changed
  in a way Onbot now leaves alone or had to repair (a room a server admin blocked, a lobby closed by
  hand, an alias another room took, a restored marker). A report is not repeated while nothing about
  it changes.

### What an admin should know

- **It is on by default** (`admin_room.enabled`), because it is where Onbot asks before mass removals
  (`!confirm <id>`) and reports what needs a human. It needs at least one admin in `admin_user_ids`
  or `authentik_group_pks_granting_bot_admin`; a config without one is refused at startup. Turn the
  room off with `admin_room.enabled: false`: reports then only reach the log, and paused removals
  can only be let through by raising the guard's limits. Keep the allowlist short, since everyone on
  it can message every user on the server.
- **Authorisation is an allowlist, never a room power level.** The allowlist is the union of
  `admin_user_ids` (full Matrix IDs listed by hand: the break-glass floor that keeps working even
  when Authentik is unreachable) and the members of the Authentik groups in
  `authentik_group_pks_granting_bot_admin`. Group membership is checked on each user Authentik
  returns, not only through Authentik's group filter, so a server that ignored the filter cannot
  make everybody an admin. When the union is empty at runtime (the admin group lost
  its last member) nobody may command the bot, even someone who is somehow in the room.
- **Authentik superusers are deliberately *not* bot admins**, and there is no option to make them so:
  a capability that pages everyone must not widen because somebody was granted an unrelated role
  upstream.
- **The set is live.** Removing somebody from the granting group revokes their commands within a
  refresh interval, without a restart; they keep their seat in the room and can still read it.
- **It is found by its marker, not by its alias.** Onbot uses only a room it created as its control
  room for this `admin_room.alias`; changing the alias setting later makes Onbot create a second,
  empty room rather than rename the first. If the alias belongs to a room Onbot did not create (or to
  another of its rooms), Onbot never posts, invites or takes commands there: the control room stays
  off, with an error in the log naming the alias. The room is unencrypted and unfederated, so the bot
  can read it and it cannot be commanded from off-server.
- **It is repaired like the other rooms.** At startup and after every reconcile pass Onbot restores
  the control room's marker if somebody at its own level removed or rewrote it, raises the marker's
  protection again if it was lowered, and puts back a removed alias (or reports once that another
  room took it). It restores the marker from what it last wrote itself, the invitation record and the
  pinned help included, and never takes over what a rewritten marker says.
- **Broadcasts are fail-soft.** They go out as quiet `m.notice` messages, and one unreachable room is
  reported rather than allowed to silence the announcement for everyone else. Markdown is rendered
  like in the welcome messages (`**bold**`, links). The `onbot broadcast` CLI command does the same
  job without a room.
- **Broadcasts follow the homeserver's rate limit.** Onbot lifts Synapse's send limit for the bot at
  startup; `!status` says whether that worked. When it did not (no admin rights, the admin API not
  reachable), the homeserver answers with "too many requests" and the broadcast slows down to the pace
  it allows instead of failing: one message at a time, as long as the server asks to wait. A few
  hundred users can then take many minutes (Synapse's default is one message per 5 seconds after a
  burst of 10). Only when nothing gets through for 5 minutes does it give up on the rest. The result
  says how often it was slowed down.
- **`!announce` answers twice.** First "Sending the announcement to N rooms", then the result when the
  last room is done; the control room keeps taking commands meanwhile. One announcement runs at a
  time: a second `!announce` before the result is refused, not queued, so send it again afterwards.
  The result names at most 20 failed rooms and counts the rest by cause; every room is in the log.
- **Edits are not commands.** Correcting a typo in an `!announce` you already sent does nothing (the
  announcement went out as first written); Onbot says so in the room. Send a new `!announce` if
  needed.
- **The same announcement twice asks first.** An `!announce` with the same text (spacing aside) from
  the same admin within 60 seconds of their previous one is not sent: Onbot answers with an id, and
  only `!confirm <id>` from that admin within 10 minutes sends it (`!reject <id>` drops it). A message
  pasted twice, or sent again because the first answer was slow, does not page everybody twice. The
  id is matched in any case, like a guard batch id, never repeats an open batch's id, and `!pending`
  lists the held announcements beside the paused batches.
- **A shutdown interrupts an announcement.** Stopping the bot cancels a broadcast in flight; Onbot
  posts how many rooms it reached. An `!announce` sent while the bot shuts down is not sent, and Onbot
  says so in the room.
- **Admins who leave stay out.** An admin who declines the invitation or leaves the room is not
  invited again on the next reconcile. To get back in, they need to drop out of the admin list (or
  group) and be added again, which brings a new invitation. Admins who left before this behaviour
  existed get one more invitation after the upgrade.
- **Broadcasts reach mapped users who are still in their notice board.** Only users the reconciler
  maps (the same set that gets welcomed) are announced to, so an account that is not in Authentik
  never receives one, even when an older Onbot gave it a notice board. `!announce` uses the set of
  the last pass and is refused until the first pass has mapped anybody; `onbot broadcast` reads
  Authentik and the identity record itself (read-only), so it needs Authentik reachable. When
  Authentik returns nobody in the sync scope (an outage, an API token that cannot see the users, a
  typo in a scope filter), the broadcast is refused too ("no users are mapped", exit code 1) rather
  than reported as "sent to 0 rooms". A user who
  left their notice board is skipped and counted apart ("1 skipped"), so the "sent to" count is the
  number of users who can read the message. Notice boards of deactivated (erased) accounts and of
  accounts on other homeservers are dropped from the bot's `m.direct` on the next pass.

**Settings:** all the fields under [`admin_room`](CONFIG_REFERENCE.md). The rationale for admitting a
command-reading room at all is in [ADR-0010](adr/0010-admin-control-room.md).

---

## Cleaning up orphaned rooms

**In one sentence:** when a group that had a room disappears (deleted, or no longer opted into chat),
Onbot can close that room down, so no stale, unmanaged room is left behind.

### The problem it solves

If a group goes away, its room should not linger as an orphan that Onbot no longer governs but users
still sit in. Closing it keeps the Matrix side a faithful mirror of Authentik, with nothing left over
that nobody owns.

### What a user sees

- A room you were in becomes closed to new activity, and you are removed with a short message saying
  the group behind it is no longer synced from the central directory.
- If that group later comes back, the room is reopened and managed again (unless a server admin
  blocked it in the meantime: Onbot only lifts its own blocks).

### What an admin should know

- **It is off by default** (`disable_rooms_when_mapped_authentik_group_disappears`). When on, a room
  whose mapped group has vanished is blocked and every member is kicked with an explanatory reason.
- **"Disappears" is broader than "deleted."** A group that loses the attribute, name prefix or parent
  that opted it into chat counts as gone too, and its room is closed the same way.
- **Only Onbot's own rooms are ever touched.** A room is recognised as obsolete by an internal stamp
  Onbot writes recording which group it belongs to; rooms without that stamp (unrelated rooms, the
  onboarding notice boards) are structurally out of reach.
- **Deletion is a further, irreversible step.** `delete_disabled_rooms` (off by default) also deletes
  the room through the Synapse admin API instead of only blocking it, destroying its history for good.
  Leave it off unless you are sure. From three deletions in one pass on, they wait for an
  administrator's `!confirm` (see [guard against mass removals](#guard-against-mass-removals)).
- A group's [visitor lobby](#visitor-lobby) is torn down together with its group room, so a blocked
  room is never left beside a still-open lobby. When the group comes back, both are unblocked.
- **A closed room is closed once.** A room Onbot has blocked and emptied is not closed again on later
  passes, so rooms closed long ago never count against the guard's limit. Members Onbot cannot remove
  (an extra room creator, or somebody at Onbot's own level in a room older than version 12, common in
  migrated rooms) are reported when the room is closed and do not make it count as open again. With
  `delete_disabled_rooms` switched on later, such a room is still deleted (under the guard).
- **A room somebody else blocked is a hold.** When a server admin (or anybody other than Onbot)
  blocked the room, for moderation or a legal hold, Onbot neither removes its members nor deletes it
  while that block exists, even when its group is gone and `delete_disabled_rooms` is on. It reports
  the room once in the log and the admin control room, and the room does not count against the
  guard. Once the block is lifted, the room is closed (and deleted, if configured) as usual.
- **Teardowns go through the guard.** If Authentik suddenly returns no groups, or far fewer than
  before, no room is closed until an administrator confirms (see
  [guard against mass removals](#guard-against-mass-removals)).

**Settings:** `disable_rooms_when_mapped_authentik_group_disappears` and `delete_disabled_rooms` under
[`sync_matrix_rooms_based_on_authentik_groups`](CONFIG_REFERENCE.md).

---

## Guard against mass removals

**In one sentence:** when a pass looks like it would remove far more than usual, Onbot pauses the
removals and asks an administrator in the admin control room instead of carrying them out.

### The problem it solves

Onbot mirrors Authentik, so a wrong answer from Authentik is mirrored too. An API token whose role was
narrowed answers with only some users and no error; a mapping attribute renamed by mistake makes every
user unmappable; a filter edited to match nobody empties the scope. Without a guard, one pass would
kick everybody from every group room, take away every moderator level and, after the grace period,
lock every account that now looks deleted.

### What a user sees

- Nothing, as long as Authentik answers plausibly.
- When the guard trips, nobody is removed: people stay in their rooms, keep their levels and keep
  their accounts until an administrator has looked at it.

### What an admin should know

- **Three checks, before anything is removed.** Each pass first works out everything it would remove:
  kicks, withdrawals of power levels Onbot granted, lowered power levels of members who stay (for
  example 100 to 50 when a power-level group drops out of the answer), teardowns of rooms whose group
  disappeared and their deletion, account locks and erasures. Then:
  - *Plausibility:* the number of active users in the sync scope and the number of groups mapped to
    rooms are compared with the last answer Onbot trusted. An empty answer, or a drop of more than
    `max_drop_percent` (20 % by default), pauses removals: fewer users pauses *every* removal of the
    pass, fewer groups pauses what happens in rooms (kicks, lowered levels, teardowns) but not account
    locks, which do not depend on groups. Each number moves with every pass in which it looked
    normal, so removing people a few at a time never trips it.
  - *Volume:* more than `max_reversible_actions_per_pass` (10 by default) kicks, locks, teardowns,
    closings of lobbies that were switched off, and lowered or withdrawn power levels in one pass
    pauses all of them.
  - *Irreversible actions:* `confirm_irreversible_actions_from` (3 by default) or more account
    erasures and room deletions in one pass pause all of them; one or two run on their own. The batch
    report marks them as irreversible. Once an erasure ran, the deletion of the account's media that
    continues over later passes needs no new confirmation. A room is only deleted in a pass that also
    closes it (blocks it and kicks its members): while that closing waits for a confirmation, so does
    the deletion, even one already confirmed.
- **Everything else goes on.** Rooms are still created and named, users still added and welcomed,
  locked users who come back still unlocked.
- **Paused actions become a batch with a short id**, for example `k7m3qp`, posted in the admin control
  room with what dropped (from how many to how many) and who or what would be affected. In the room:
  - `!pending` lists the open batches, rejected ones included (marked as rejected, with the time
    they end at the latest);
  - `!confirm <id>` releases exactly that batch. It runs at once, and only what still applies is done:
    somebody who is back in Authentik meanwhile is left alone. A confirmation never covers more than
    the batch showed; actions that came up later get a batch of their own. After a confirmed drop the
    new numbers count as normal. A confirmed batch that cannot run within `confirmation_expiry_sec`
    of its confirmation (for example a room deletion that keeps waiting for the closing of its room)
    is closed and reported; its actions are then judged again like any others, so one that still
    needs a confirmation is proposed again under a new id;
  - `!reject <id>` holds the batch without running it, and it is not proposed again while the
    situation that created it lasts (for example a broken token that you are about to replace). The
    rejection ends when that situation is over (the numbers look plausible again, or the pass no
    longer has too many removals with these), at the latest when the batch expires. Its actions that
    still apply are then judged again like any others: one user who really was deleted meanwhile is
    locked as usual, a still implausible answer pauses them in a new batch. The end is reported in
    the room. Irreversible actions in a rejected batch (erasures, room deletions) are the exception:
    they stay held until the batch expires, also when fewer of them remain than
    `confirm_irreversible_actions_from` (one or two would otherwise run on their own). When the rest
    of the batch ends earlier, that is reported as a partial end.
- **A batch expires** after `confirmation_expiry_sec` (72 hours by default). Its actions stay paused
  meanwhile; if they still apply afterwards, a later pass proposes them again under a new id.
- **An unreadable guard state** (the bot's account data `<reversed server name>.onbot.guard` edited
  by hand, or written by a newer Onbot) pauses *every* removal: the guard has lost its last known
  good numbers and the rejected batches, so it cannot judge anything. This is reported once in the
  control room and the state is never overwritten. After checking the Authentik side, an admin runs
  `!guard reset`: it copies the unreadable state verbatim to
  `<reversed server name>.onbot.guard.unreadable.<unix time>` in the bot's account data, starts a new
  state that keeps the last known good numbers where they could still be read, and runs a pass.
  Open batches are gone after that; what still needs a confirmation is proposed again under a new
  id.
- **Without the admin control room** nobody can confirm. Paused actions are then only logged
  (`guard: paused ...` at warning level, each action at debug level) and stay paused; raise the limits
  if a large change is intended.
- **Preview a pass** with `onbot reconcile-once --dry-run`: it reads Authentik and Matrix, logs every
  change it would make (prefixed `[dry-run]`), including what the guard would pause, and changes
  nothing. See [deployment.md](deployment.md#cli-commands).

**Settings:** the fields under [`destructive_action_guard`](CONFIG_REFERENCE.md).
</content>
