# Troubleshooting

| Symptom | Likely cause |
|---|---|
| Users never get added to rooms | The MXID localpart contract is broken. `authentik_username_mapping_attribute` does not match MAS's localpart template, so computed MXIDs do not exist. See [configuration.md](configuration.md#the-mxid-localpart-contract). |
| Disabled users keep their Matrix access | `mas_admin` is not configured (the Synapse admin API cannot revoke a MAS session), or the lifecycle `dry_run` is still `true` (the default). |
| Nothing destructive ever happens | Expected by default. The lifecycle is quarantined (`dry_run: true`) and only logs to the `onbot.lifecycle.audit` channel until you opt in. |
| Welcome DM send fails with a 500 | The bot device is not registered yet. Onbot registers it on startup (`ensure_device_registered`); check the startup logs. |
| `healthcheck` reports a dependency FAIL | Read the per-dependency log line; it distinguishes unreachable from auth-rejected. The `matrix-cs` line also flags a token or `bot_user_id` mismatch. |
| Sliding sync unavailable | The homeserver does not advertise MSC4186. Onbot falls back to the reconciler signal path automatically. |

For the healthcheck details, see [deployment.md](deployment.md#healthcheck). For the auth topology
behind several of these, see [architecture.md](architecture.md).
