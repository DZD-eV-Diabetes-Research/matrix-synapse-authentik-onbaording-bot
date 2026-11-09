# Known issues

Small, self-contained defects found in passing and not yet fixed. Larger work packages live in
[`todo.md`](todo.md); architectural decisions live in [`docs/adr/`](docs/adr/). The findings of the
2026-11-04 code review and the sessions that fix them are in
[`docs/review_26-11-04/`](docs/review_26-11-04/SESSIONS.md).

Each entry states the symptom, where it lives, and what a fix would look like.

---

No open entries.

---

## Fixed

- ~~Lobby rooms never re-sync their name or topic~~ (a renamed group, or a changed
  `visitor_lobby_name_suffix` / `visitor_lobby_topic_template`, left the lobby's old wording in
  place). Fixed in 0.0.10: the per-room pass converges the name and topic of the group room and its
  lobby alike (`_converge_room_attributes` in `onbot/reconciler/engine.py`), covered by
  `test_lobby_name_and_topic_follow_the_group_room`.
- ~~Group room topics were rewritten on every reconcile pass~~ (the desired topic was compared against
  `MatrixRoom.topic`, which the admin List Room API never populates). Fixed in 0.0.7: the topic is
  read from `m.room.topic` state before writing, and the field is gone from `MatrixRoom`.
- ~~A per-group `keep_updating_matrix_attributes_from_authentik` override was ignored~~ (the engine read
  the flag from `matrix_room_default_settings` only). Fixed in review session S2: it is resolved per
  group through `resolve_room_settings`, like every other field of `MatrixDynamicRoomSettings`, and
  applies to the group room and its lobby.
