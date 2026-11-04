# Known issues

Small, self-contained defects found in passing and not yet fixed. Larger work packages live in
[`todo.md`](todo.md); architectural decisions live in [`docs/adr/`](docs/adr/).

Each entry states the symptom, where it lives, and what a fix would look like. None of these are
release blockers (for those, see the security items in [`CHANGELOG.md`](CHANGELOG.md)).

---

## 1. Lobby rooms never re-sync their name or topic

**Symptom:** a visitor lobby gets its name and topic once, at creation, and keeps them forever. Rename
the Authentik group (or change `visitor_lobby_name_suffix` / `visitor_lobby_topic_template`) and the
group room follows on the next reconcile while its lobby silently keeps the old wording. The two rooms
sit side by side in the space listing, so the drift is visible to every user.

**Where:** [`onbot/reconciler/engine.py`](onbot/reconciler/engine.py) around lines 444-447. The
per-room convergence loop calls `_converge_room_attributes(gm)`, which asserts on `gm.room` and only
ever touches the group room. Lobbies get `_converge_lobby_membership_and_join_rules` instead, which
covers membership and the join rule but not the room's identity.

**Fix sketch:** generalise `_converge_room_attributes` to take a `MatrixRoom` plus a
`RoomCreateAttributes` rather than a `GroupRoomMap`, then call it a second time with
`(gm.lobby, gm.lobby_desired)` when a lobby exists. The desired lobby attributes are already computed
in `compute_lobby_attributes`, so nothing new has to be derived. Watch two things:

- the lobby's name is derived from the *group room's* name at creation time
  ([`rooms.py:173-174`](onbot/reconciler/rooms.py#L173-L174)), so a lobby whose group room is renamed
  gets a new desired name for free once this is wired up;
- the topic must be read from `m.room.topic` room state, not from `MatrixRoom`, for the reason
  recorded in the `MatrixRoom` docstring (the admin List Room API returns no topic). The group-room
  path already does this, so a shared helper inherits it.

Worth a test that a lobby whose group room is renamed picks up the new name and suffix on the next
pass.

---

## Fixed

- ~~Group room topics were rewritten on every reconcile pass~~ (the desired topic was compared against
  `MatrixRoom.topic`, which the admin List Room API never populates). Fixed: the topic is read from
  `m.room.topic` state before writing, and the field is gone from `MatrixRoom`. See the `Fixed`
  section of [`CHANGELOG.md`](CHANGELOG.md).
- ~~A per-group `keep_updating_matrix_attributes_from_authentik` override was ignored~~ (the engine read
  the flag from `matrix_room_default_settings` only). Fixed: it is resolved per group through
  `resolve_room_settings`, like every other field of `MatrixDynamicRoomSettings`, and applies to the
  group room and its lobby.
