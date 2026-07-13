# ADR-0012 — Visitor lobby rooms: an open front door in front of a closed group room

- **Status:** Accepted (2026-07-13)
- **Context:** Operators want a group's room to be *visitable* — someone outside the Authentik group
  should be able to find it and walk in — without exposing the group's private history or its
  membership. Opening an existing group room cannot do this: `history_visibility` defaults to
  `shared`, so the first visitor to an unencrypted group room can scroll back to the day it was
  created. That is a change you cannot safely make on a live deployment.

## Decision

Leave the group room **exactly as private as it is today** — same `join_rule: invite`, same history,
same encryption, same membership (a pure projection of the Authentik group, kicks and all, ADR-0002
intact). Beside it the bot maintains a **second room, the lobby**, which starts empty (no history to
leak) and which every member of the parent space may find and join of their own accord.

* **Visibility falls out of the existing space-hierarchy filter, not new code.** With the group room
  at `join_rule: invite` and the lobby at `join_rule: restricted` allowing the parent space, a space
  member who is not in the group sees exactly one of the two rooms in the space listing — the lobby —
  and can join it; a group member is joined to both. The lobby's `m.space.child` is written with
  `suggested: true`; the private group room's with `suggested: false`.
* **The lobby is `restricted` to the space, resolved at runtime.** Its `m.room.join_rules` content is
  `{"join_rule": "restricted", "allow": [{"type": "m.room_membership", "room_id": "<space_id>"}]}`,
  where the space id is resolved from the space alias at reconcile time — which is why the rule cannot
  be static config. It is set as `initial_state` at creation (never briefly invite-only nor briefly
  open) and re-converged by a join-rule reconcile pass (`onbot/reconciler/join_rules.py`) over
  existing lobbies too, so an operator can close a lobby again by editing it. No room version is
  pinned: `restricted` needs room version 8+ and `knock_restricted` 10+, but the bot asserts a floor
  and lets the room inherit the server default (now v12, ADR-0011); pinning a number would freeze the
  bot behind the ecosystem.
* **Lobby defaults differ from the group room's, deliberately.** Unencrypted by default (a room open
  to the whole space gains little from encryption, which would greet every visitor with "unable to
  decrypt"; encryption is irreversible once on, so the operator opts in before the room exists).
  Preset power levels so visitors can talk (`users_default`/`events_default` 0) with the bot at 100
  and `state_default` 100 so only the bot rewrites room state. Group members are seeded in by default
  (`visitor_lobby_inject_group_members`) — an empty lobby is a dead lobby — but this is a social bet
  the operator can decline.

## The carve-out from ADR-0002, and why it needs no new state

ADR-0002 makes Authentik the source of truth and the reconciler **total** over room membership:
desired = the group's members, and anything else is drift to be removed. **A lobby is the first room
where that is deliberately false.** Its membership is bot-injected group members *plus* people who
exist only in Matrix (visitors who joined on purpose), and the reconciler **adds without ever
removing** (`diff_room_membership(..., kick_enabled=False)`).

This carve-out is confined to lobby rooms and needs no ledger of who-was-added, because **"never
kick" is a strictly weaker invariant than "kick only whom we added"** — there is nothing to
remember when you never remove. What removes a departing human from a lobby is **account
deactivation**, which drops the account from every room server-side (`onbot/lifecycle/accounts.py`),
not the reconciler. A user removed only from the Authentik group is kicked from the group room and
**keeps their lobby seat**, now indistinguishable from any other visitor. That is what a lobby is for.

## The single most dangerous failure, and its guard

A lobby mistaken for a group room: discovery would project Authentik membership onto it and **kick
every visitor** on the next tick. The guard is a distinct `OnbotRoomType.visitor_lobby` state event
(`onbot/reconciler/state.py`) stamped on the lobby at creation and a separate `GroupRoomMap.lobby`
slot — the lobby is never the `GroupRoomMap.room` the membership projection iterates. The unit test
that asserts a lobby never enters the group-room membership diff was written before the code that
could have caused it (todo §6).

## Consequences

- **The room's *existence* is not secret, only its contents are.** The `m.space.child` on the space
  carries the private room's id, and any space member can read space state; any user can resolve
  `#group:server` to a room id through the directory. A non-member still cannot join it, read its
  history, see its name/topic, or enumerate its members — but a curious person with `curl` can learn
  a room with that id exists. If that is unacceptable the answer is a *second space*, not this
  feature. Documented in `docs/troubleshooting.md`.
- **A lobby requires the parent space.** `restricted` needs a space to be restricted *to*;
  `visitor_lobby_enabled` while `create_matrix_rooms_in_a_matrix_space` is off is rejected at
  config-validation time (a lobby that silently stayed invite-only would be worse than a startup
  error). A lobby enabled at runtime via an Authentik attribute while no space is configured is
  skipped with a warning rather than created invite-only.
- **The lobby is torn down with its group room.** When the mapped Authentik group disappears,
  `disable_rooms_when_mapped_authentik_group_disappears` blocks the lobby alongside the group room —
  a blocked group room beside a live, joinable lobby is the worst of both.
- **Encryption is irreversible in both directions of this decision** — you cannot turn it off on a
  group room that has it, and you cannot turn it on retroactively without recreating the room. The
  weaker lobby default is chosen so the operator decides before the room exists. Documented in
  `docs/troubleshooting.md`.
</content>
