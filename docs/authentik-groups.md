# Configuring Authentik groups

This page is written from the **Authentik side**: you have a group, you want it to become a Matrix
room, and you want to know exactly what to type into that group's **Attributes** box in the Authentik
admin UI.

Everything else in the docs describes Onbot's config file. This page describes the other half of the
contract, the part your group owners touch. Nothing here needs a config deploy: an attribute set in
Authentik takes effect on the next reconcile.

- Want the full list of Onbot config fields instead? See [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md).
- Want the "what does this feature do" story? See [features.md](features.md).

---

## The short version

Open the group in Authentik (*Directory > Groups > your group > Edit*), and put this in
**Attributes**:

```yaml
# Opt this group into chat (only if you configured the attribute filter, see "Step 1")
is_chatroom: true

# Room topic
chatroom_topic: The Düsseldorf site channel. Coffee, logistics, fire drills.

# Room icon, downloaded and re-uploaded to your homeserver
chatroom_avatar_url: https://intranet.company.org/img/duesseldorf.png

# Every member of this group gets power level 50 (moderator) in every managed room
chat-systemwide-powerlevel: 50

# Open a public "front door" lobby beside this private room
chatroom_visitor_lobby: true

# RARELY NEEDED. Raw Matrix `POST /createRoom` parameters, as a JSON *string*. Leave this out
# unless one specific group needs a room shaped differently from every other room. See Step 5.
chatroom_params: '{"preset": "public_chat", "visibility": "public"}'
```

Every one of those keys is a **default that you can rename** in Onbot's config. The rest of this page
says what each does, what values are legal, and which config field renames it.

A normal group needs the first four at most. `chatroom_params` is an escape hatch for one-off cases
and most deployments never set it on any group.

---

## Attribute reference

These are the attributes Onbot reads off a group, with their default names:

| Attribute in Authentik | Value type | What it does | Config field that renames it |
|---|---|---|---|
| `is_chatroom` (example only) | any, matched exactly | Opts the group in, if you enabled the attribute filter | `sync_matrix_rooms_based_on_authentik_groups.only_groups_with_attributes` |
| `chatroom_topic` | string | The room's topic | `matrix_room_default_settings.matrix_topic_from_authentik_attribute` |
| `chatroom_avatar_url` | HTTP(S) URL string | The room's icon | `sync_matrix_rooms_based_on_authentik_groups.room_avatar_url_attribute` |
| `chat-systemwide-powerlevel` | integer 0 to 100 | Power level granted to this group's members | `sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level` |
| `chatroom_visitor_lobby` | boolean | Opens or suppresses a [visitor lobby](features.md#visitor-lobby) for this group | `matrix_room_default_settings.matrix_room_visitor_lobby_from_authentik_attribute` |
| `chatroom_params` **(niche, rarely needed)** | **string** containing JSON | The Matrix [`POST /createRoom`](https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3createroom) request body (preset, visibility, federation, history visibility), read once when the room is created. Only for a group that must be shaped differently from the rest of the deployment | `matrix_room_default_settings.matrix_room_create_params_from_authentik_attribute` |
| `chatroom_name` (opt-in) | string | Room display name, if you point the name at an attribute instead of the group name | `matrix_room_default_settings.matrix_name_from_authentik_attribute` |
| `chatroom_alias` (opt-in) | string | Room alias localpart, if you point the alias at an attribute instead of `pk` | `matrix_room_default_settings.matrix_alias_from_authentik_attribute` |

Two of those are **not** attributes by default: the room's **name** comes from the group's `name`
field and its **alias** comes from the group's `pk`. You only need `chatroom_name` / `chatroom_alias`
if you deliberately repoint them (see Step 2).

### The path gotcha: when to write `attributes.`

Onbot config fields that name a group attribute come in two flavours, and mixing them up is the most
common way to have an attribute silently ignored:

**Paths from the group object's root**, so a custom attribute needs the `attributes.` prefix:

- `matrix_topic_from_authentik_attribute` (default `attributes.chatroom_topic`)
- `matrix_name_from_authentik_attribute` (default `name`, a built-in group field)
- `matrix_alias_from_authentik_attribute` (default `pk`, a built-in group field)
- `matrix_room_create_params_from_authentik_attribute` (default `attributes.chatroom_params`)
- `matrix_room_visitor_lobby_from_authentik_attribute` (default `attributes.chatroom_visitor_lobby`)

**Paths already inside `attributes`**, so the prefix must be left off:

- `authentik_group_attr_for_matrix_power_level` (default `chat-systemwide-powerlevel`, may itself be
  dotted to nest, e.g. `synapse-options.chat-powerlevel`)
- `room_avatar_url_attribute` (default `chatroom_avatar_url`, a flat key lookup: a dot in this value
  is part of the key name, it does not descend)

The names you write in Authentik never carry the `attributes.` prefix. That prefix only ever appears
in Onbot's config file.

---

## Step 1: opt the group in

By default **every** Authentik group becomes a room, which is almost never what you want on a real
directory. Pick one of these three ways to opt groups in, in Onbot's config:

```yaml
sync_matrix_rooms_based_on_authentik_groups:
  # (a) a custom attribute, the most flexible: group owners opt in themselves
  only_groups_with_attributes:
    is_chatroom: true

  # (b) a group name prefix, no attributes needed (the prefix stays part of the room name)
  only_for_groupnames_starting_with: "chat-"

  # (c) direct children of a parent group, by the parent's pk (immediate children only)
  only_for_children_of_groups_with_uid: ["a1b2c3d4parentgroupuid"]
```

Notes:

- With **(a)**, the group attribute is matched on **exact value**. `is_chatroom: true` in the config
  matches a group carrying `is_chatroom: true`, not `is_chatroom: "yes"` and not a group that merely
  has the key. Filtering happens on the Authentik API side.
- Multiple filters are **combined**, so a group must satisfy all the ones you set.
- `authentik_group_id_ignore_list` (a list of group `pk`s) is applied first and wins over everything,
  including a group that carries the opt-in attribute.
- The attribute name `is_chatroom` is an **example**, not a built-in. Whatever key/value pair you put
  in `only_groups_with_attributes` is the one your groups must carry.

## Step 2: name, alias, topic and icon

**Name.** Comes from the group's `name` field. To take it from an attribute instead, set
`matrix_name_from_authentik_attribute: attributes.chatroom_name` and give the group a
`chatroom_name`. An optional `name_prefix` in the config is prepended.

**Alias.** Comes from the group's `pk`, deliberately: a Matrix alias **can never be changed after the
room is created**, and a `pk` survives the group being renamed. Only repoint it at
`attributes.chatroom_alias` if you accept that renaming that attribute later strands the old room and
builds a fresh, empty one. Dashes are stripped from the alias.

**Topic.** Comes from the `chatroom_topic` attribute. Leave the attribute unset for no topic. One
wrinkle: if the config sets a `topic_prefix` and the group has no topic attribute, the room's topic
becomes just the prefix.

**Icon.** Put an HTTP(S) URL in `chatroom_avatar_url`. Onbot downloads it and re-uploads it to your
homeserver's media repository, and only when the URL changed, so pointing it somewhere new updates
rooms that already exist. A group's visitor lobby wears the same icon automatically.

Name and topic are re-applied on every reconcile while
`keep_updating_matrix_attributes_from_authentik` is on (the default), so edits made in a Matrix client
are overwritten. Turn it off to let room admins rename their own rooms.

## Step 3: power levels

Put an integer from 0 to 100 in the group's `chat-systemwide-powerlevel` attribute. Every member of
that group gets that power level **in every room Onbot manages**, not only in that group's own room.
That is what "systemwide" means, and it is why the usual pattern is a small, separate group:

```
Group "Moderators"          attributes:  chat-systemwide-powerlevel: 50
Group "Chat Admins"         attributes:  chat-systemwide-powerlevel: 100
```

Details worth knowing:

- The value must be a real **integer**. A string (`"50"`) or a boolean is ignored.
- When a user's groups disagree, the **highest** value wins.
- Authentik **superusers** are set to 100 regardless, while
  `make_authentik_superusers_matrix_room_admin` is on (the default).
- Matrix's own conventions: 0 default user, 50 moderator, 100 admin.
- A power level is only applied to a user in rooms they are actually a member of. To give somebody
  access and admin rights in *every* room including ones they are not in, that is the separate and
  much sharper `global_room_admins` config list, not a group attribute.

## Step 4: visitor lobby

Set `chatroom_visitor_lobby: true` on a group to open an [open front-door room](features.md#visitor-lobby)
beside its private room. Set it to `false` to suppress a lobby the config would otherwise give it.
Leave it unset to inherit `matrix_room_default_settings.visitor_lobby_enabled`.

- The value must be a boolean (`true` / `false`). The strings `"true"` and `"false"` are also
  accepted. Anything else is logged as a warning and the configured default applies.
- Lobbies require the parent space (`create_matrix_rooms_in_a_matrix_space`) to be enabled. Onbot
  refuses to start otherwise.

## Step 5: room creation parameters (`chatroom_params`), niche

> **Skip this unless you have a specific, unusual requirement for one group.**
>
> `chatroom_params` is a raw escape hatch into the Matrix API, not a normal knob. A healthy
> deployment sets it on **no groups at all**: the deployment-wide
> `matrix_room_default_settings.default_room_create_params` already shapes every room, and if you
> want *all* rooms different, change that instead. Reach for the group attribute only when exactly
> one group must differ from the rest, for example a single public room in an otherwise private
> deployment.
>
> The cost of using it: the values are unvalidated, they are read **once** at room creation and can
> never be applied again, several of them (federation, encryption, room version) are **irreversible
> for the lifetime of that room**, and a mistake can create a room that leaks history or that nobody
> can moderate. If a config-level setting can express what you want, prefer it.

### What it actually is

`chatroom_params` is **not an Onbot format**. It is the request body of the Matrix Client-Server API
call that creates the room:

> **`POST /_matrix/client/v3/createRoom`**
> [spec.matrix.org/latest/client-server-api/#post_matrixclientv3createroom](https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3createroom)

Whatever that endpoint accepts, you can put here, minus the handful of keys Onbot fills in itself
(listed below). If you want to know what a key does or which values are legal, the spec page above is
the authority, not this document. Useful companions:

| Topic | Where the spec defines it |
|---|---|
| `createRoom` request body, every key | [POST /createRoom](https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3createroom) |
| What each `preset` expands to | [Room creation presets](https://spec.matrix.org/latest/client-server-api/#creation) |
| `visibility`, the public room directory | [Room directory](https://spec.matrix.org/latest/client-server-api/#published-room-directory) |
| `power_level_content_override` contents | [m.room.power_levels](https://spec.matrix.org/latest/client-server-api/#mroompower_levels) |
| `initial_state` events, e.g. history visibility | [m.room.history_visibility](https://spec.matrix.org/latest/client-server-api/#mroomhistory_visibility) |
| `creation_content`, e.g. disabling federation | [m.room.create](https://spec.matrix.org/latest/client-server-api/#mroomcreate) |
| Room versions | [Room version specification](https://spec.matrix.org/latest/rooms/) |

Synapse-specific behaviour (rate limits, `m.federate`, directory publishing rules) is documented at
[element-hq.github.io/synapse](https://element-hq.github.io/synapse/latest/).

### How the final request body is built

Three layers, in this order:

1. **Onbot's config default**, `matrix_room_default_settings.default_room_create_params`, which ships
   as `{"preset": "private_chat", "visibility": "private"}`.
2. **The group's `chatroom_params`**, merged over it. The merge is **shallow**: a top-level key you
   set replaces that key wholesale, it is not deep-merged.
3. **Onbot's own keys**, written last, which win over both.

So `chatroom_params` is how one group departs from the deployment default, for example a single
public room in an otherwise private deployment.

### Keys Onbot sets itself

Setting these in `chatroom_params` has no effect, because Onbot overwrites them after the merge:

| Key | Onbot sets it from |
|---|---|
| `room_alias_name` | the computed alias (Step 2) |
| `name` | the group name or `chatroom_name` (Step 2) |
| `topic` | `chatroom_topic` (Step 2), **but only if that attribute is set**, otherwise a `topic` here survives |
| `initial_state` | yours is **kept and appended after** Onbot's own events (the space-parent link, and `m.room.encryption` when encryption is on), so a same-typed event of yours wins rather than being dropped |
| `room_version` | `synapse_server.room_version`, **but a `room_version` here wins over it** |

### Keys worth setting

```yaml
# A public, world-readable room for one group, in an otherwise private deployment
chatroom_params: '{"preset": "public_chat", "visibility": "public"}'

# Keep a room off this homeserver's federation (irreversible, decided at creation)
chatroom_params: '{"creation_content": {"m.federate": false}}'

# Let anyone who joins later read the full history
chatroom_params: '{"initial_state": [{"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "world_readable"}}]}'

# Raise the bar for posting: only moderators (50+) may send messages
chatroom_params: '{"power_level_content_override": {"events_default": 50}}'
```

Two warnings on those:

- **Avoid `"preset": "trusted_private_chat"`.** It grants every invitee power level 100, permanently
  and un-demotably. Onbot's own DM rooms deliberately avoid it for that reason.
- **Do not name the bot in a `power_level_content_override` `users` map.** Under room version 12 the
  room creator holds an infinite power level and must be *absent* from `m.room.power_levels`; naming
  it there is rejected by the auth rules and the room creation fails. See
  [ADR-0011](adr/0011-room-version-12.md).

### Caveats

- **Creation-time only.** The parameters are read exactly once, when the room does not exist yet.
  Editing `chatroom_params` afterwards changes nothing about the existing room, and there is no
  "recreate" path short of deleting the room.
- **It does not apply to a visitor lobby.** A lobby is built from its own fixed parameters (private
  preset, its own power levels, its join rule set at creation). `chatroom_params` shapes the group
  room only.
- **`invite` is a trap.** Users you invite here are not in the Authentik group, so the next membership
  sync removes them again if `kick_matrix_room_members_not_in_mapped_authentik_group_anymore` is on.
  Group membership is the only supported way to put people in a room.

### The YAML/JSON trap

**The value must be a string containing JSON**, not a YAML mapping. Authentik's attributes box is
YAML, so the natural way to write it is the wrong one:

```yaml
# WRONG: parsed as a YAML mapping, Onbot logs "invalid room-create-params JSON" and ignores it
chatroom_params:
  preset: public_chat

# RIGHT: a string that happens to contain JSON
chatroom_params: '{"preset": "public_chat", "visibility": "public"}'
```

Wrap the whole JSON object in single quotes, and use double quotes inside it, as JSON requires. If
you get this wrong the room is still created, just with the deployment defaults, and the warning
naming your group is in the log.

---

## What is *not* a group attribute

- **Who is in the room.** That is the group's membership. Onbot mirrors it, add and remove people in
  Authentik.
- **Per-group settings that have no attribute.** Encryption, name/topic prefixes and the lobby's
  naming are set per group in Onbot's config under
  `per_authentik_group_pk_matrix_room_settings`, keyed by the group's `pk`. Only the keys you list
  there are overridden.
- **The parent space** and **welcome messages**. Deployment-wide config, see
  [features.md](features.md).
- **User-level attributes.** `authentik_username_mapping_attribute` (the MXID contract) and
  `sync_only_users_with_authentik_attributes` sit on *users*, not groups. See
  [configuration.md](configuration.md#the-mxid-localpart-contract).

## Checking your work

Attributes take effect on the next reconcile. To see the result immediately:

```bash
onbot reconcile-once --log-level DEBUG
```

If a group did not become a room, walk this list:

1. Is its `pk` in `authentik_group_id_ignore_list`? That wins over everything.
2. Does it satisfy *every* filter you configured in Step 1 (attribute value matched exactly, name
   prefix, parent group)?
3. Is the attribute name in Authentik exactly the one Onbot is looking for, with no `attributes.`
   prefix in Authentik itself?
4. For topic / lobby / create-params, does the **config** value carry the `attributes.` prefix? For
   power level and avatar, does it correctly **omit** it?

Onbot logs a warning naming the group for a malformed lobby value and for invalid create-params JSON.
A wrongly typed power level is skipped silently, so check the type if a moderator stays at 0.

More symptom-to-cause entries are in [troubleshooting.md](troubleshooting.md).
