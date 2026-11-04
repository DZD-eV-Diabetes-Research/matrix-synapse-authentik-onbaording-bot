# 08 Live verification against the local integration stack

Findings from files 01, 03, 04, 05 and 06 checked on the real stack from `tests/integration/stack/`
(Synapse 1.156.0 with room version 12 default, MAS, Postgres, Authentik 2026.5.3), commit `2c2e3a1`.
Everything ran on the workstation. Onbot source and existing tests were not modified.

## Baseline

* First attempt: `ONBOT_ITEST_KEEP=1 ./run_integration_tests.sh` exited **0** with `377 passed, 17 skipped`.
  All 17 integration tests were skipped because `docker compose up` failed on stale exited containers of
  the `onbot-itest` project from three months ago ("network ... not found"). The coverage gate still passed
  (90.12 % >= 88 %). See LIV-01.
* After `docker compose down -v` on the stale project and a fresh `up`:
  `394 passed in 406.80s`, total coverage 93.91 %. The stack was kept running for the checks below.

## Verdicts

| Finding | Verdict | Evidence (one line) |
|---|---|---|
| LIF-01 | confirmed live | Locked user deleted in Authentik: next pass calls MAS unlock, ledger entry dropped, old token 200 again |
| SEC-01 | confirmed live | Same run: deleted user unlocked, `createRoom` with the pre-lock token succeeds, nothing ever re-locks |
| LIF-07 | confirmed live | Re-enabled user: MAS `locked_at` null, pre-lock access token back to 200 within 3 s |
| LIF-13 | refuted | After a MAS lock Synapse reports `locked: false`, user still listed by `/v2/users?guests=false`, third pass is a no-op |
| REC-01 | partially (worse in one respect) | Zero-permission token gets 403 (safe); an object-scoped RBAC token gets 200 with a 1-user list, and one pass kicked every group member |
| SEC-02 | confirmed live | Private squat room: every pass aborts with 403; public world-readable squat room: group member force-joined into the attacker's room |
| REC-04 | confirmed live | PL-50 moderator clears canonical alias: every pass aborts with `M_ROOM_IN_USE`; deleting the alias strands the old room with members |
| REC-05 | confirmed live | v10 and v11 rooms: demotion rejected ("remove ops level equal to your own"), kick rejected, pass aborts every tick |
| SEC-06 | confirmed live | MAS-only account (not in Authentik) invited into a group room gets a notice board, 3 welcome messages, and the next broadcast |
| RUN-03 / ONB-14 | confirmed live (trigger nuance) | With an unknown connection pos the pump logged 20 failures in ~95 s, kept the same pos, `!status` never answered; a Synapse restart does not invalidate pos |
| ONB-06 | confirmed live | With `encryption_enabled_by_default_for_room_type: invite` control room and notice boards carry `m.room.encryption`; an `m.room.encrypted` command is ignored, nothing logged |
| LIF-11 | confirmed live | Body-less MAS `POST /users/{id}/deactivate` returns 200; Synapse user `erased: true` with `include_user_media_on_delete: false` |
| RUN-02 | confirmed live | Cold start with Synapse down: device never registered (404), admin room never created, notice board created but 0 messages, 20 HTTP 500 |
| RUN-09 | confirmed live | Docker: stop during startup 10.2 s exit 137; steady state 3 of 5 stops exit 137. Plain process: 28.9 s graceful, or instant death during startup |
| ONB-01 / RUN-01 | confirmed live | 20 concurrent first welcomes: 9 entries lost from `m.direct`; after a restart pass those 9 users have 2 notice boards and 6 welcome messages |

Additional live evidence for findings not on the list: REC-03 and LIF-04 (one poisoned room aborts the whole
pass, lifecycle included; see check 9) and LIF-02 (deleted users are never offboarded; see check 1).

New findings: LIV-01 to LIV-03 at the end.

All experiment scripts and logs are in
`/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/`
(shared helpers in `live_common.py` there; run from the repo root with
`PYTHONPATH=<that dir> .venv/bin/python <that dir>/<script>`). Each section names its script by full path.

---

## Check 1: LIF-01 / SEC-01 / LIF-07 (and LIF-13, check 2)

Script: `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check1_lifecycle_unlock.py`, log `check1.log` in the same directory.

Steps: provision `alice` and `bob` via a real MAS login (keep their access tokens), disable both in Authentik,
run `run_reconcile_once` with lifecycle `enabled: true, dry_run: false, deactivate_after_n_sec: 0,
delete_after_n_sec: null`.

| Pass | alice (MAS / ledger / old token) | bob |
|---|---|---|
| 1 mark | `locked_at: null`, entry with `marked_for_disabling_timestamp`, whoami 200 | same |
| 2 lock | `locked_at: 2026-11-04T11:46:12Z`, `disabled_user_timestamp` set; whoami 200 at first, **401 after 115.9 s** (Synapse introspection cache) | locked, 401 |
| 3 | unchanged, still locked, whoami 401 (no oscillation) | unchanged |
| Then: alice `DELETE /core/users/{pk}/` (204), bob `is_active: true` | | |
| 4 | **`locked_at: null`**, ledger entry **null**, old token **200** (after 114.8 s cache) | **`locked_at: null`**, ledger null, old token **200** after 3 s |
| 5 | still unlocked, ledger still empty: nothing will lock alice again | |

With the revived token alice could act, not only read: `POST /createRoom` returned
`200 {"room_id": "!A0JcKrCApv6QQEhDYeBlsBdUwM9wyeWW4BCZvlIMmaQ"}`.

So LIF-01 scenario 1 and SEC-01 are confirmed end to end, including the MAS behaviour that was only
documented before: unlock brings back the exact pre-lock access token. LIF-07 is confirmed for the legitimate
re-enable path: bob's pre-disable token works again, so a disable/password-reset/re-enable cycle does not
end a stolen device's session. LIF-02 is visible too: after the unlock, alice (gone from Authentik) is in
neither the active nor the disabled list and is never offboarded.

Note on timing: lock and unlock both take effect for existing tokens only after Synapse's introspection
cache lapses (about 2 minutes, observed 115.9 s and 114.8 s). The audit line "logged out ... (revoked all
sessions)" is written immediately (see LIV-03).

### Check 2: LIF-13 (same run)

* After the lock: `GET /_synapse/admin/v2/users?guests=false` still lists alice (also with `locked=true`).
  `GET /_synapse/admin/v2/users/@alice...` shows `locked: false, deactivated: false`. MAS does not propagate
  its lock to Synapse's `locked` flag on this version pair.
* Pass 3 after the lock: MAS `locked_at` unchanged, ledger unchanged, token still 401. No flip.

Verdict: **refuted** for Synapse 1.156 + this MAS. The fix sketch (`locked=true`, `deactivated=true` in the
listing) is still cheap insurance, because the lifecycle depends on that list (LIF-01 scenario 6): after an
erase the user does disappear from the default listing (check 9).

## Check 3: REC-01

Scripts (same directory): `check3_probe.py`, `check3_probe2.py`, `check3_probe3.py`, `check3_kick.py`,
full path of the main one:
`/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check3_kick.py`.

Authentik `2026.5.3`:

* `groups_obj` is still embedded in `/core/users/` (also with `include_groups=false`).
* Token of the `service_account` endpoint: intent `app_password`, every API call 403 `Token invalid/expired`.
* API-intent token for a user with **no permissions** (service account or internal user):
  `GET /core/users/?is_active=true` and `?is_active=false` and `GET /core/groups/` all return
  **403** `You do not have permission to perform this action.` The client raises, the pass fails, no kicks.
  The finding's literal trigger ("token loses view permission -> 200 empty list") does **not** happen.
* API token whose role has **object-level** `authentik_core.view_user` on one user only:
  `GET /core/users/?is_active=true` -> **200**, `count: 1` (only `akadmin`); `?is_active=false` -> 200,
  `count: 0`. A narrowed role silently collapses the list.

Kick experiment: group `livrbac...-room` with two provisioned members; one normal pass (members joined).
Then a pass with a token whose role has global `view_group` and object-level `view_user` on `akadmin` only:

```
members after normal pass:   [@livrbacm1..., @livrbacm2..., @onbot]
narrowed pass:               completed (no error)
members after narrowed pass: [@onbot]
members after full-token pass: [@livrbacm1..., @livrbacm2..., @onbot]   (force-joined back)
```

Verdict: **partially confirmed**. A token with no rights fails safe (403). A token whose rights were narrowed
to some objects (a realistic RBAC tightening, or a role scoped to a subset) passes silently and one pass
kicks every member of every group room. With lifecycle enabled the same collapse empties the disabled list
too, which per LIF-01 scenario 5 unlocks every locked user (not run separately; the mechanism is the one
shown in check 1).

## Check 4: SEC-02 / REC-04

Script: `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check4_squat.py`, log `check4.log`.

Config: `matrix_alias_from_authentik_attribute: attributes.chatroom_alias` (a documented example), so the
future alias is known. The attacker is an ordinary MAS-provisioned user.

**A, private squat room** (attacker `createRoom` with `room_alias_name`, default preset: 200). Then the
Authentik group with that alias and a member is created. Both passes:

```
ApiError("GET .../rooms/!e0MR.../state/test.onbot.onbot.group_room -> HTTP 403:
 {'errcode': 'M_FORBIDDEN', 'error': 'User @onbot:onbot.test not in room ..., and room previews are disabled'}")
```

A third pass with an additional, unrelated group aborts the same way, and the unrelated group's room is
**never created** (alias lookup 404). One squatted alias stops room creation, membership and lifecycle for
the whole server. The squat room keeps only the attacker.

**B, public, world_readable squat room**: both passes raise

```
ApiError("POST .../rooms/!-p-E.../kick -> HTTP 403: {'errcode': 'M_FORBIDDEN', 'error': '@onbot:onbot.test not in room ...'}")
```

but before that, the admin join API **force-joined the group member into the attacker's room**:
squat room members `[@livattacker..., @livsqm...]`, `creator: @livattacker...`, `join_rules: public`. The bot
itself never joins (so it cannot moderate), and the pass aborts when it tries to kick the attacker. An
unrelated new group's room is created here (room creation runs before the membership stage), but its
membership is never converged in the aborted pass.

**C, canonical alias cleared by a moderator** (group with `chat-systemwide-powerlevel: 50`; PL map shows
`m.room.canonical_alias: 50`, the member at 50). The member `PUT`s `m.room.canonical_alias` with `{}`: 200.
Both following passes:

```
ApiError("POST .../createRoom -> HTTP 400: {'errcode': 'M_ROOM_IN_USE', 'error': 'Room alias already taken'}")
```

The alias still resolves to the original room. **C2**: the same PL-50 moderator deletes the directory alias
(`DELETE /directory/room/#...`: 200). The next pass completes and creates a second room for the group; the old
room is stranded with `[@livmod..., @onbot]` still joined.

Verdict: SEC-02 and REC-04 points 1 to 3 **confirmed live**, including the force-join into an attacker room.

## Check 5: REC-05

Scripts: `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check5_legacy_pl.py` and `check5b_demote_only.py` (same directory), logs `check5.log`, `check5b.log`.

`synapse_server.room_version` set to `"10"` and `"11"` (the config supports it). Two Authentik users in
`global_room_admins`: `gin` (also a group member) and `gout` (not a member). After the grant pass the room
reports `room_version: "10"` / `"11"` and `users: {gin: 100, gout: 100, @onbot: 100}`.

Then `global_room_admins: []`:

* With `gout` present, every pass raises
  `POST .../kick -> HTTP 403: {'errcode': 'M_FORBIDDEN', 'error': 'You cannot kick user @livgout...'}`.
* Demotion only (5b, just `gin`): every pass raises
  `PUT .../state/m.room.power_levels -> HTTP 403: "You don't have permission to remove ops level equal to your own"`.
  PL map unchanged (`gin: 100`).
* A new unrelated group added in the same pass gets its room, but its member is never joined
  (members `[@onbot]`): the pass aborts first.
* The ex-admin cannot demote the bot either (same 403), so the room ends up with two equal owners.

Verdict: **confirmed live** for v10 and v11, exactly as described, including the permanent pass abort.

## Check 6: SEC-06

Script: `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check6_outsider_welcome.py`, log `check6.log`.

The outsider is a MAS-local account created with `mas-cli manage register-user` (never in Authentik, the
"MAS-only user" case of the finding; the federated case cannot be tested in the hermetic stack). A group
member invites it into the group room (200), it joins (200). Then one real sync slice
(`sliding_sync(None)`) is fed to `OnboardingListener.handle_sync`, exactly as the pump does:

```
outsider in bot m.direct:       present, rooms ["!7Zje..."]
outsider is a broadcast target: true
outsider can read welcome messages: 200, 3 bot messages (security key hint, guide link, welcome text)
broadcast summary: "sent to 34 rooms, 0 failed"; outsider received the broadcast: true
next reconcile pass: outsider kicked from the group room; still in m.direct: true
```

Verdict: **confirmed live** for local non-Authentik accounts. The notice board survives the kick, so the
account keeps receiving every later announcement.

## Check 7: RUN-03 / ONB-14

Scripts: `check7_probe.py`, `check7_probe2.py`, and
`/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check7_pump.py`, log `check7.log`.

What Synapse 1.156 answers:

| Situation | Response |
|---|---|
| `docker compose restart synapse`, then the old pos | **200**, stream continues (connections are persisted in Postgres) |
| Malformed pos (`garbage`, `999999/1`) | 400 `M_UNKNOWN` `Invalid stream token` |
| Well-formed pos with an unknown connection position (`99999/s1116_...`) | 400 `M_UNKNOWN_POS` `Unknown position` |
| Valid pos after deleting the `sliding_sync_connections` rows | 400 `M_UNKNOWN_POS` |

Pump test: engine, pump and discovery loops in-process with the admin control room. `!status` from the
allowlisted admin is answered in 1.1 s. Then the pump's stored pos is replaced at its next request by the
same token with connection position `99999` (the response a purged or expired connection produces). A second
`!status` got **no reply within 90 s**; the log has 20 `sync failed; backing off 5s` tracebacks, every one
`HTTP 400 M_UNKNOWN_POS`, and `pump._pos` stayed at the bogus value. (A first attempt that deleted the DB rows
under a running long-poll did not break the live connection, Synapse keeps it in memory.)

Verdict: **confirmed live** that the pump never recovers from `M_UNKNOWN_POS`. Different from the description:
a Synapse restart is **not** a trigger here. Triggers are server-side connection expiry or purge and DB
restores. A fix should also reset on the `M_UNKNOWN` "Invalid stream token" case, not only `M_UNKNOWN_POS`.

## Check 8: ONB-06

Script: `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check8_default_encryption.py`, log `check8.log`.

Temporary stack change: appended `encryption_enabled_by_default_for_room_type: invite` to
`tests/integration/stack/synapse/homeserver.yaml`, restarted Synapse. **Reverted** afterwards with
`git checkout` (byte-identical to the saved copy) and Synapse restarted.

```
control room m.room.encryption:     200 {"algorithm": "m.megolm.v1.aes-sha2"}
new notice board m.room.encryption: 200 {"algorithm": "m.megolm.v1.aes-sha2"}
bot welcome messages in the notice board: plaintext m.room.message in an encrypted room
plaintext "!status" (client ignoring room encryption): answered
m.room.encrypted event sent by the admin (what Element sends there): accepted (200), not answered
onbot log lines mentioning encryption: none
```

Verdict: **confirmed live**. No real E2EE client was used; the encrypted command is a syntactically valid
`m.room.encrypted` event, which is what any E2EE client sends in such a room.

## Check 9: LIF-11

Script: `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check9_erase.py`, logs `check9.log` and `check9_first_attempt_aborted.log`.

User with display name "Erase Me", disabled in Authentik; lifecycle `deactivate_after_n_sec: 0,
delete_after_n_sec: 0, include_user_media_on_delete: false`; three passes.

```
pass2: MAS locked_at set
pass3: POST http://localhost:8080/api/admin/v1/users/01M6K6XS.../deactivate "HTTP/1.1 200 OK"
       audit: "erased @liverase...:onbot.test (account deactivated, media=False)"
Synapse user: deactivated: true, erased: true, displayname: null
profile: {"errcode": "M_UNKNOWN", "error": "No row found (profiles)"}
```

Verdict: **confirmed live**. MAS accepts the body-less POST and GDPR-erases in Synapse although media
deletion is off. (This MAS version's user resource exposes no `deactivated_at`, only `locked_at`.)

Side evidence for REC-03 / LIF-04: the first attempt ran without a group filter and aborted in
`_converge_room_membership_and_levels` on a kick in one of the v10 rooms from check 5. The lifecycle stage
never ran, so the disabled user was not even marked. One unrelated broken room silently disables offboarding
for everyone.

## Check 10: RUN-02

Scripts: `check10_probe.py` and
`/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check10_cold_start.py`, onbot log `check10_onbot.log`.

Probe first: a fresh MAS compat token for the bot (keys never uploaded) can create rooms, but
`PUT /rooms/.../send/m.room.message/...` returns **500** `M_UNKNOWN Internal server error`. The docstring claim
holds.

Cold start: fresh compat token, `docker compose stop synapse`, start the real CLI `onbot run` (config with
admin room enabled, tick 10 s), start Synapse after 15 s (healthy 3 s later), provision a new user, wait 45 s.

```
startup steps failed: version negotiation, device registration, rate-limit override, admin control room
  (13:02:12 to 13:02:22, about 3.5 s each)
new user's notice board: created (["!fv9g..."]), bot messages in it: 0
"welcome flow failed" for that user: 5 times in 45 s; "500 Internal Server Error": 20 log lines
admin control room alias: 404 (never created)
bot device row in Synapse: 404 (never registered)
```

Verdict: **confirmed live**. The process stays degraded although every dependency is up. The user is
force-joined into an empty "Announcements" room and the bot retries the send every tick.

## Check 11: RUN-09

Logs: `check11_docker_*.log`, `check11_proc_startup.log` (same directory); the steady-state process number
comes from check 10.

Docker (image built locally from the repo Dockerfile in 14 s, tag removed afterwards; container on the stack
network, config pointing at `synapse:8008` etc.):

| Case | `docker stop` duration | Exit code |
|---|---|---|
| During startup (Synapse stopped, 4 s after start) | 10.2 s | **137** (SIGKILL, PID 1 ignored SIGTERM) |
| Steady state, stopped after 25 s | 8.4 s | 0 |
| Steady state, after 17 s / 33 s / 41 s | 10.2 s each | **137** |
| Steady state, after 52 s | 9.8 s | 0 |

Plain process (`onbot run`, not PID 1):

* SIGTERM during startup: exit 143 after 0.005 s, killed by the default handler mid-startup with no cleanup.
* SIGTERM in steady state: graceful exit 0 after **28.9 s** (the 30 s sliding-sync long-poll); the
  in-process stop in check 7 took 29.0 s.

Verdict: **confirmed live**. The 10 s Docker budget is missed whenever the long-poll has more than about 10 s
to run, and SIGTERM during startup is ignored under Docker.

## Check 12: ONB-01 / RUN-01

Script: `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/live/check12_mdirect_race.py`, log `check12.log`.

20 users provisioned into one group; a first pass with `welcome_new_users_messages: []` joins them to the
group room without notice boards. Then engine plus sync pump run for 60 s (the pump's initial sync replays
the 20 joins while the reconcile pass emits `user_synced` for the same users). Then one more
`run_reconcile_once` simulates a restart.

| | After 60 s service run | After restart pass |
|---|---|---|
| Users with an `m.direct` entry | 11 of 20 | 20 |
| Users with a room carrying onbot `direct_room` state | 20 | 20 |
| Lost (room exists, no `m.direct` entry) | **9** | 0 |
| `direct_room` rooms not referenced by `m.direct` | 9 | 9 |
| Users with 2 notice boards | 0 | **9** |
| Welcome messages per user | 3 for all 20 | 3 for 11 users, **6 for 9 users** |

Verdict: **confirmed live**, at a higher rate than the unit experiment suggests (45 % of the batch).

---

## New findings

### LIV-01 Integration runner exits 0 with every integration test skipped

- **Severity:** medium
- **Category:** test
- **Location:** tests/integration/conftest.py:78-83 (`pytest.skip` on `compose.start()` failure), run_integration_tests.sh
- **Status:** confirmed live (baseline, first attempt)
- **Effort:** S

When `docker compose up` fails (here: stale containers referencing a deleted network), the session fixture
turns it into `pytest.skip`. `./run_integration_tests.sh`, documented as "the authoritative coverage gate",
then reports `377 passed, 17 skipped`, exit 0, and the coverage gate passes at 90.12 % because 88 % is
reachable without the integration tier. A CI job or a developer reading only the exit code believes the live
suite ran. Fix sketch: fail instead of skip when the run is explicitly the integration run (for example an
`ONBOT_ITEST_REQUIRED=1` set by the script and CI), and run `docker compose down` before `up` (or use a
unique project name per run) so stale state cannot break startup.

### LIV-02 Audit line says "erase in Nones" when erasure is disabled

- **Severity:** low
- **Category:** bug
- **Location:** onbot/lifecycle/accounts.py:291-293
- **Status:** confirmed live (check 1 log)
- **Effort:** S

With `delete_after_n_sec: null` the lock audit line reads
`logged out @livalice...:onbot.test (revoked all sessions); erase in Nones`. Cosmetic, but it is the
irreversible-action audit channel. Fix: print "erase disabled" when the value is `None` (and fix the
"revoked" wording per LIF-07).

### LIV-03 Lock and unlock reach existing tokens only after about 2 minutes, the audit line is immediate

- **Severity:** info
- **Category:** docs
- **Location:** onbot/lifecycle/accounts.py (audit messages), docs on offboarding
- **Status:** confirmed live (check 1: 115.9 s until 401 after lock, 114.8 s until 200 after unlock)
- **Effort:** S

Synapse caches MAS token introspection (known, see `tests/integration/stack_api.py::wait_revoked`). Operators
reading "logged out ... (revoked all sessions)" should know a live client keeps working for up to about two
minutes. Worth one sentence in the offboarding docs; no code change needed.

## Cleanup

* Stack torn down with `docker compose down -v` (it was not running before: the old project containers were
  in "exited" state and broken). The temporary image `onbot-review-tmp:live` was removed.
* `tests/integration/stack/synapse/homeserver.yaml` was changed temporarily for check 8 and restored with
  `git checkout`.
* No files were added under `tests/`. Throwaway tokens were deleted from the scratchpad; the config files left
  there only contain credentials of the destroyed test stack.
