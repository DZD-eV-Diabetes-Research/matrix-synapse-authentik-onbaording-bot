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

For the healthcheck details, see [deployment.md](deployment.md#healthcheck). For the auth topology
behind several of these, see [architecture.md](architecture.md).

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
