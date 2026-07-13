# Troubleshooting

| Symptom | Likely cause |
|---|---|
| Users never get added to rooms | The MXID localpart contract is broken. `authentik_username_mapping_attribute` does not match MAS's localpart template, so computed MXIDs do not exist. See [configuration.md](configuration.md#the-mxid-localpart-contract). |
| Disabled users keep their Matrix access | `mas_admin` is not configured (the Synapse admin API cannot revoke a MAS session), or the lifecycle `dry_run` is still `true` (the default). |
| Nothing destructive ever happens | Expected by default. The lifecycle is quarantined (`dry_run: true`) and only logs to the `onbot.lifecycle.audit` channel until you opt in. |
| Welcome DM send fails with a 500 | The bot device is not registered yet. Onbot registers it on startup (`ensure_device_registered`); check the startup logs. |
| `healthcheck` reports a dependency FAIL | Read the per-dependency log line; it distinguishes unreachable from auth-rejected. The `matrix-cs` line also flags a token or `bot_user_id` mismatch. |
| Sliding sync unavailable | The homeserver does not advertise MSC4186. Onbot falls back to the reconciler signal path automatically. |
| A user can still write in their welcome room | Their room predates onbot's read-only welcome room. See below. |
| A user is in their welcome room without ever accepting an invitation | Expected: `force_join_onboarding_room` is on by default. |
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
(`DELETE /_synapse/admin/v1/rooms/<room_id>` with `purge`) and drop its entry from the bot's
`m.direct` account data, after which the next reconcile recreates it correctly. Be aware that this
also re-sends every welcome message, because the per-message bookkeeping lives in the state of the
room being destroyed — on a live server it pages every user it touches.
