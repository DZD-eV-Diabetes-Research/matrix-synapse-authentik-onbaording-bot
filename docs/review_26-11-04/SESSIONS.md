# Fix sessions from the 2026-11-04 review

Each block below is a cold-start brief for one working session (roughly one focused day or less).
Start a session by pointing Claude at this file and the session ID, for example:
"Work on session S1 from docs/review_26-11-04/SESSIONS.md".

## Rules for every session

- Read `README.md`, `DECISIONS.md` and the detailed write-ups of the listed finding IDs in the area
  files (`01` to `08`) before changing code. The write-ups contain failure scenarios, file:line
  locations and fix sketches; experiment scripts referenced there live in an old scratchpad and may be
  gone, so rebuild what you need as real tests.
- Every fixed finding gets a regression test that fails before the fix. Destructive paths also get an
  integration test where the stack can express them.
- Use `pdm run` (pdm is the only tool since S0). Keep `pdm run pre-commit run --all-files` and
  `pdm run pytest tests/unit tests/contract -q` green (pre-commit covers ruff, ruff format and mypy). Run `./run_integration_tests.sh` (or a `-k` subset) for sessions that touch
  Matrix, MAS or Authentik behaviour; since S0 a skipped stack fails the run. Use your own stack
  instance with `export ONBOT_ITEST_INSTANCE=<n>` (see `tests/README.md`) and tear it down at the end
  with `docker compose -f tests/integration/stack/docker-compose.yml -p onbot-itest-<n> down -v`.
- Update `CHANGELOG.md` (Unreleased) and the user docs the change affects. Behaviour changes to
  destructive features must be called out under a "Changed" or "Security" heading.
- At the end, mark the session done in the status table below and note anything left over.
- No em dashes or en dashes in docs, comments or commit messages. No AI attribution in commits.
- Ask the maintainer (AskUserQuestion) when a step says "confirm", or when a fix needs a product
  decision not covered by `DECISIONS.md`.

## Recommended order and status

| Order | Session | Topic | Size | Depends on | Status |
|---|---|---|---|---|---|
| 1 | S0 | Tooling baseline | S | | done (e3282ef) |
| 2 | S1 | Offboarding fails open (critical hotfix) | M | S0 | done (merged in wave 1a) |
| 3 | S2 | Reconciler pass robustness | M | S0 | done (merged in wave 1a) |
| 3a | W1F | Wave 1 review fixups (D14, startup 4xx, reconcile-once, base image) | S | S1, S10 | done (merged in wave 1c) |
| 3b | CI1 | CI duration: integration path filter, release reuses the CI result (D17) | S | S15, S16 | done (merged before wave 2) |
| 7a | W2F | Wave 2 fixups: test stack MAS 1.28 and Synapse 1.164, LIF-09 refusal, admin room default (D19) | S | S3, S4 | done (merged in wave 2) |
| 7b | W2R | Wave 2 review fixups: guard fails closed, rejected batches end, ledger report, guard gaps | S | S3, S4 | done (merged in wave 2) |
| 11a | W3R | Wave 3 review fixups: identity guess vs MAS owner, marker sender, upgrade claims, lock retry, batch expiry | S | S5, S6, S9 | done (merged in wave 3) |
| 11b | W4R | Wave 4 review fixups: closed rooms, human-blocked rooms as hold (D23), lost notice boards, upkeep isolation, binding wins (D21) | S | S7, S8 | open |
| 4 | S3 | Guard layers 1 and 2, reconciler dry-run | M | S2 | done (merged in wave 2) |
| 5 | S16 | Git history scrub | S | S0, no open branches | done (force-pushed 2026-11-06, new main 89afecf) |
| 6 | S4 | Offboarding completeness | M | S1 | done (merged in wave 2) |
| 7 | S5 | Guard layer 3: `!confirm` for irreversible actions | M | S3, S4 | done (merged in wave 3) |
| 8 | S6 | Room identity (marker plus creator) and unified room provisioning | L | S2 | done (merged in wave 3) |
| 9 | S7 | Room repair: space link, lobbies, blocks | M | S6 | done (merged in wave 4) |
| 10 | S8 | Onboarding correctness | M | S6 | done (merged in wave 4) |
| 11 | S9 | User identity across renames | M | S1, S3 | done (merged in wave 3) |
| 12 | S10 | Runtime lifecycle: startup, sync recovery, shutdown, liveness | M | S0 | done (merged in wave 1a) |
| 13 | S11 | HTTP layer: retries, idempotency, token provider | M | S10 | done (merged in wave 2) |
| 14 | S12 | Broadcast and admin room | S | S8 | done (merged in wave 4) |
| 15 | S13 | Security hardening and config validation | M | S0 | done (merged in wave 1b) |
| 16 | S15 | CI and release hardening | M | S0 | done (merged in wave 1b) |
| 17 | S14 | Docs, dead code, cleanup | S | last | open |
| 18 | S17 | Bot-driven room upgrades (D20) | M | S6, S7 | deferred: later iteration, not before the next release |

S1 to S3 should land before the next release: together they close every known way the bot can
mass-kick users or revive offboarded accounts. The history scrub (S16) is placed early because every
later commit would otherwise have to be rewritten too; it needs a moment with no open branches or PRs.
S10, S13 and S15 are independent of the room and identity work and can be pulled forward freely.

## Parallel execution in waves

The sessions run as subagents, at most 3 at a time, each in its own git worktree on its own local
branch `review/<session>-<slug>` (for example `review/s1-offboarding`). The orchestrating session
merges the branches into `main` locally after each wave, one at a time, running the unit suite after
every merge and the integration suite once per wave. Nothing is pushed; the maintainer pushes.

| Wave | Sessions | Notes |
|---|---|---|
| 0 | S0 | Alone. Also makes the integration stack runnable in parallel (per-worktree compose project name and host ports). |
| 1a | S1, S2, S10 | S1 and S2 touch different parts of `engine.py`. |
| 1b | S13, S15 | |
| 1c | W1F | Fixups from the wave 1 review, merged before wave 2. |
| between 1 and 2 | S16 | History scrub, only when no review branch is open. |
| 2 | S3, S4, S11 | |
| 3 | S5, S6, S9 | S6 is the largest session. S12 moved to wave 4 (overlaps with S8, depends on it). |
| 4 | S7, S8, S12 | |
| 5 | S14 | |
| later | S17 | Not part of this review run; not before the next release (D20). |

After each wave a fresh reviewer agent reads the merged diff of that wave cold (without the
orchestrator's summary) and checks that every listed finding is really fixed and nothing regressed.

**Rules for session agents (in addition to the rules above):**

- Work only on your session's findings. If a fix needs a change that belongs to another session, note
  it in your final report instead of doing it.
- Do not edit `CHANGELOG.md` and do not regenerate `docs/CONFIG_REFERENCE.md` or `config.example.yml`.
  Put the changelog lines into your final report; the orchestrator writes them and regenerates the
  config docs after merging.
- Do not edit this file's status table; report the status instead.
- Product decisions not covered by `DECISIONS.md`: stop and report the question instead of guessing.
- Commit on your branch in small, reviewable commits. No push, no AI attribution, no em or en dashes.
- Use your own integration stack instance (see `tests/README.md` after S0) and tear it down at the end.

---

## W4R Wave 4 review fixups

From the cold review of wave 4 (`git diff b5fd0b5 a47c0f1`, S7 and S8), 2026-11-09. No critical
finding. W4R-05 (empty mapped set, broadcast exits 0) went to S12.

- **W4R-01 (medium):** a closed room is only recognised when the bot blocked it and only the bot is
  left (`_closed_by_the_bot`, `_plan_teardown` in `engine.py`). `_disable_room` skips unkickable
  members (v12 `additional_creators`, humans at PL 100 in pre-v12 rooms, typical for migrated rooms),
  so such a room plans `room_teardown` on every pass and counts against layer 2; from 11 of them every
  pass trips layer 2. Fix: closed means blocked by the bot and every remaining member is the bot or
  unkickable (or an equivalent durable record).
- **W4R-02 (medium, D23):** a room blocked by someone other than the bot is a hold. No kick, no
  deletion while that block exists, reported once in the admin room and the log, and it does not
  count against layer 2 every pass. Today the teardown kicks everyone and `delete_disabled_rooms`
  purges it. `test_teardown_keeps_a_block_a_server_admin_set` asserts the kick and must change.
- **W4R-03 (low):** ONB-08 is not fixed when a user kicked the bot from their notice board. Synapse
  serves a departed user's state reads from the state at leave time (`allow_departed_users=True`);
  only purge, forget or ban answer 403. The stale `m.direct` entry stays and every welcome fails with
  403. The unit fake in `tests/unit/test_welcome.py` models the wrong behaviour. Decide "board lost"
  from the bot's membership (`joined_rooms()`) or a 403 on send. Prove the Synapse behaviour with an
  integration test (kick the bot from a board, expect a new board).
- **W4R-04 (low):** when `get_room_block` fails, a human-blocked room is fully managed (REC-11 fails
  open): `gm.room` is set before `_converge_block`. Skip membership, power levels and upkeep while the
  block status is unknown.
- **W4R-06 (low):** marker repair only works while the room identity is cached in the process; after
  a restart a room whose marker was removed or rewritten is skipped and never repaired. Report such a
  group clearly (admin room and log, with what a human has to do) and document the limit; no
  automatic re-adoption (D20). Make the repair test fakes serve the same state to scan and effector.
- **W4R-07 (low):** room upkeep is all-or-nothing and shares the guarded "space" step with joining
  users into the space: one refused upkeep write stops new users joining the space on every pass;
  for group rooms the avatar is skipped when an earlier repair is refused. Run space upkeep as its
  own step and isolate each repair.
- **W4R-08 (low):** marker restore takes its content from the wrong source (`repair.py`): a fresh
  marker drops `global_admins` and `avatar_source_url`; a foreign same-role marker is re-signed
  verbatim. Restore from the cached bot-written `RoomIdentity.marker_content`.
- **D21 addition:** when MAS is not configured or does not link the account, the holder of a recorded
  binding wins over claimants that only resolve by the mapping attribute; those are skipped and
  reported.

Experiment tests from the review (asserting the faulty behaviour) are in the orchestrator's
scratchpad, `review-w4/test_w4_experiments.py`; turn them into real regression tests.

---

## CI1 CI duration (D17)

**Goal:** shorter waits without weakening the gate. Runs after S16 (the scrub), before wave 2.

- Path filter: the integration job runs only when `onbot/`, `tests/`, `pyproject.toml`, `pdm.lock`,
  `Dockerfile`, `.dockerignore`, `run_integration_tests.sh` or `.github/workflows/` change. Required
  checks must still report on docs-only PRs (a skipped job inside the workflow, not a workflow-level
  `paths` filter, so branch protection does not wait forever).
- Release: look up a successful `ci.yml` run for the tagged SHA (`gh api` with the run's `head_sha`,
  event `push` on `main`) and skip the `workflow_call` CI when one exists and covered the integration
  job; otherwise run the CI as today. Keep the tag-on-main check. Test the decision logic.
- Update `docs/development.md` (release flow, how long a release takes).

---

## W1F Wave 1 review fixups

**Goal:** close what the cold review of waves 0 and 1 found before wave 2 starts.

- D14: a known user (`KnownUsers` binding) who is disabled and out of scope at the same time is
  offboarded normally (today: no lock, no report; `accounts.py` `_classify_one` returns frozen and
  `decide_account_action` turns that into `none` without a ledger entry).
- RUN-02 behind a reverse proxy: `runtime.is_permanent_failure` treats every 4xx except 401, 408, 425
  and 429 as final, so a proxy 404 or 403 during a host reboot gives up device registration and opens
  onboarding anyway. Treat 4xx without a Matrix `errcode` as transient.
- RUN-12: the avatar fetcher's HTTP client is never closed (`app.py` passes it into `MediaUploader`,
  which does not own it).
- `reconcile-once` dropped setting the bot avatar and provisioning the admin room (re-invites);
  cron-only deployments need both. Restore them as startup steps and fix the stale docs that say it
  does not lift the rate limit (`app.py` docstrings, `docs/deployment.md`).
- `scripts/provision-bot-account.sh --revoke-previous` revokes before issuing: issue first, revoke
  only after the new token is parsed.
- Base image from bookworm to trixie (D15), same Python patch, tag and digest; update the docs.
- Config descriptions of `sync_authentik_users_with_matrix_rooms.enabled` (since S2) and
  `global_room_admins` (cap at 99 in pre-v12 rooms).

---

## S0 Tooling baseline

**Goal:** make commits, tests and the integration runner trustworthy before touching behaviour.

**Findings:** MNT-08, SUP-03, MNT-07, SUP-11, MNT-06, LIV-01, MNT-11.

**Steps:**
- Bump the ruff pre-commit hook to a version that understands `py314`; check every other hook still
  runs. Restore a gitleaks job in CI (it was removed in 05c717b). Add a `.gitleaksignore` or allowlist
  for the integration-stack fixtures (SUP-15) so the scan is green and meaningful.
- Delete `uv.lock` (pdm is canonical: CI, Dockerfile, docs). Make CI fail when `pdm.lock` is out of
  date (`pdm lock --check` or equivalent).
- Make unit tests hermetic: an autouse fixture in `tests/conftest.py` that clears `ONBOT_*` env vars
  and points `ONBOT_CONFIG_FILE_PATH` at a non-existent path. Verify by dropping a bogus `config.yml`
  into the repo root and running the suite (then remove it).
- `tests/integration/conftest.py`: a failed stack start must fail the run, not skip it. Keep an explicit
  opt-out env var for machines without Docker.
- Fix the `--log-level` position problem (MNT-11): accept it after the subcommand too, or fix the docs
  and `run_onbot_reconcile_once.sh`.
- Make the integration stack runnable several times in parallel on one machine: compose project name
  and every published host port come from environment variables (defaults unchanged), and the test
  harness reads them. Document in `tests/README.md` how to start an isolated instance per worktree.

**Done when:** `pre-commit run --all-files` passes, CI has a secret-scan job, one lockfile, the suite
is hermetic, a broken stack makes `./run_integration_tests.sh` exit non-zero, and two stack instances
with different project names run the integration suite at the same time.

---

## S1 Offboarding fails open (critical)

**Goal:** an offboarded user can never get their sessions back by accident, and deleted users are
offboarded too.

**Findings:** LIF-01, SEC-01, MNT-02, LIF-02, LIF-03, LIF-05, REC-14, LIF-06, LIF-10, LIV-02, part of
LIF-16 and MNT-05 (tests).

**Design (confirm the third bullet at session start):**
- Re-enable (MAS unlock) only when the user is positively present in Authentik, active, and in scope.
  Never infer "re-enabled" from absence in the orphan set.
- Persist a minimal "known users" record (`authentik pk -> MXID`, first seen). A user who was known and
  is now absent from Authentik is "deleted" and continues through the offboarding state machine. This
  is what makes LIF-02 safe: Synapse accounts that were never mapped (service accounts, MAS-only users)
  are never touched. S9 later extends the same record into the identity binding of D8, so design it
  with that in mind.
- Users who are still in Authentik but have left the sync scope: proposal is to freeze them (no unlock,
  no further destructive step) and report. Confirm with the maintainer.
- Until S3 lands, add the minimal guard: an empty user list from Authentik aborts the lifecycle step.
- One query (one scope definition) feeds both "active" and "disabled" sets (LIF-05, REC-14).
- Dry-run must not call any effector, including unlock; only unlock locks onbot itself recorded (LIF-06).
- Save the ledger after each user, not once at the end; a user whose step fails gets a backoff and does
  not block the others (LIF-03).
- MAS lookup 404 is an error, not success; the state machine does not advance and the audit log says so
  (LIF-10). Fix the "erase in Nones" audit line (LIV-02).

**Tests:** unit tests for `MasLifecycleEffectors`; the live scenarios from `08` check 1 (lock, then
delete in Authentik; lock, then re-enable) as integration tests asserting the old token stays dead
after delete and works only after a real re-enable.

**Done when:** the live check 1 scenarios pass as integration tests, and no code path unlocks a user
who is not positively active in Authentik.

---

## S2 Reconciler pass robustness

**Goal:** one bad room or one transient error never stops the pass, and nothing kicks users the
config says to leave alone.

**Findings:** REC-03, SEC-04, LIF-04, REC-05, REC-02, MNT-01, REC-06, REC-07, REC-08, REC-12, issues.md
entry 2 (MNT-04 part).

**Steps:**
- Per-room error isolation: each room's convergence is wrapped, the failure is logged with the room and
  group, counted, and the pass continues. The lifecycle step runs independently of room failures.
  A room the bot cannot fully manage (extra creator from `chatroom_params`, D6) is reported once per
  pass, not retried in a tight loop.
- REC-05: in pre-v12 rooms never grant PL 100 (cap at the bot's own level minus one) and stop trying to
  demote users the bot cannot demote; report them instead.
- REC-02 / MNT-01: `enabled: false` means "do not manage membership", never "everyone is unwanted". An
  empty desired member set for every room must not kick (minimal check here, full guard in S3).
- REC-06: a failed admin-group lookup keeps the previous admin set instead of treating it as empty.
- REC-07: honour `matrix_user_ignore_list` on the kick path.
- REC-08: withdraw elevated PLs the bot granted when the granting membership ends (G8.4). Only touch
  entries the bot owns.
- REC-12: never write the bot's own MXID into the PL users map.
- issues.md entry 2: read `keep_updating_matrix_attributes_from_authentik` through
  `resolve_room_settings`.

**Tests:** the live scenarios REC-03 (banned member), REC-05 (v10/v11 room) as integration tests.

**Done when:** a broken room in the live stack leaves every other room and the offboarding step working.

---

## S3 Guard layers 1 and 2, reconciler dry-run (D4)

**Decisions from 2026-11-06 (D13):** a tripped guard does not just block. It pauses the destructive
part, stores a pending batch with its own id, reports in the admin room, and `!confirm <id>` releases
exactly that batch (`!pending` lists them, `!reject <id>` closes one, 72 h expiry, no `!confirm`
without an id, re-check against the current state on confirm; details in D13). This session builds
the batch store and the three commands; S5 only adds layer 3. Defaults: layer 1 at more than 20 %
drop (users in scope or mapped groups, baseline moves with every normal pass), layer 2 at more than 10
reversible actions per pass.

**From the wave 1 review:** the withdrawal of bot-granted power levels (D10) is not guarded today. A
collapsed Authentik answer suppresses kicks but still withdraws every recorded grant in every room.
Count withdrawals as reversible destructive actions under layer 2 and put them behind the layer 1
gate. Also fix the stale config descriptions of `sync_authentik_users_with_matrix_rooms.enabled` and
`global_room_admins` if W1F has not done it.

**Goal:** a collapsed or wrong Authentik answer, or a config mistake, cannot cause mass destruction.

**Findings:** REC-01, LIF-08, MNT-09.

**Handover from wave 1:** since S1, a known user who vanishes from Authentik is treated as deleted and
locked after the cooldown. A token that suddenly sees only a few users therefore makes every other
known user look deleted; layer 1 must catch that (only an empty list is caught today). "Report in the
admin room" has no route yet: S1 emits `Signal.lifecycle_attention` (frozen users, failures, aborts)
and logs `FROZEN`, `FAILED`, `lifecycle step aborted` at WARNING. Add the admin-room subscriber in
`app.py` in this session and route the guard reports and the S2 room failure events through it.

**Handover from S2:** S2 emits `Signal.room_not_converged` (`room_kind`, `room_id`, `group_pk`,
`problems`) once per room per pass and `Signal.destructive_actions_suppressed` (`action`, `reason`);
`engine.last_pass_report` holds the last pass's problems (useful for `!status`). S2's minimal guard
skips all kicks when no group room has any desired member. With a single group room this cannot tell a
group that legitimately empties from a collapsed Authentik answer; layer 1 (comparison against the last
known good counts) should replace that heuristic. When the room stage fails as a whole, the pass still
raises after the lifecycle ran. The config descriptions of `sync_authentik_users_with_matrix_rooms.enabled`
and `global_room_admins` (the 99 cap in pre-v12 rooms) still need a wording update (S13 or S14).

**Steps:**
- Layer 1: persist last known good counts (users in scope, groups mapped to rooms). Abort the
  destructive part of a pass when the new list is empty or drops by more than a configurable
  percentage. Report in the admin room.
- Layer 2: a per-pass threshold for reversible destructive actions (kicks, MAS locks, room teardown).
  Above it, skip those actions, log loudly, report in the admin room. Choose sensible defaults and make
  both configurable; confirm the defaults with the maintainer.
- A real reconciler dry-run (`DryRunEffectors` wired in, or a flag on the engine) that logs every
  intended change; fix the `reconcile-once` docstring. Turning `dry_run` off after a long dry run must
  go through the same thresholds (LIF-08).

**Tests:** the live REC-01 scenario (token that sees one user) must result in zero kicks and a report.

---

## S4 Offboarding completeness

**From the wave 1 review:** a freeze caused by `authentik_user_ignore_list` ends when the user is
deleted in Authentik: the ignore check runs only over present users, so the deleted user falls
through to "deleted" and resumes, possibly erasing at once if the erase cooldown elapsed during the
freeze. Keep the freeze for deleted users whose ledger entry is frozen for the ignore reason. Dry-run
audit lines repeat every tick (LIF-08 noise), coordinate with S3's dry-run work.

**Goal:** offboarding does what the docs say, on both effector paths.

**Findings:** LIF-07, LIV-03, LIF-09, LIF-11 (D7), LIF-12, LIF-14, LIF-15, rest of LIF-16 and MNT-05.

**Handover from S1:** effectors already have the shape `erase(mxid, gdpr_erase=...)` plus a separate
`delete_media`; the MAS effector ignores `gdpr_erase` so far. LIF-12 and LIF-14 may be partly covered by
S1's per-user ledger save and backoff; check before re-doing.

**Steps:**
- LIF-07: on lock, also finish the user's MAS sessions (compat, OAuth2, browser) so re-enable does not
  revive them; check the MAS admin API for the right endpoints. Document the ~2 minute Synapse token
  cache delay (LIV-03).
- LIF-09: when the homeserver is MAS-backed and `mas_admin` is missing, warn loudly at startup and make
  the audit log say exactly what was done. Consider refusing the Synapse fallback under MAS.
- LIF-11: implement D7 (`gdpr_erase_on_delete`, default false, same on both paths).
- LIF-12: paginate media deletion until done, resume on the next pass.
- LIF-14: tolerate a corrupt ledger entry (quarantine it, continue), bound growth (drop entries after
  the final state plus a retention period), alert when the ledger disappears while locks exist.
- LIF-15: fix the config descriptions and ADR-0005 to match the code.

**Tests:** integration tests for the erase stage and re-enable under MAS.

---

## S5 Guard layer 3: confirmation for irreversible actions (D4)

**Handover from S3:** plug in at `decide()` in `onbot/guard.py`, after layer 1: hold irreversible
actions (`ActionKind.erase`, plus a deactivate kind if you split it, plus room deletion per D19) when
there are 3 or more in the `rest` set. The lifecycle already plans `erase`
(`AccountLifecycleManager.plan_accounts`) and runs it only if `permit(mxid, action)` allows it;
batches, confirm, expiry and reports already cover it. Add the limit to `GuardLimits` and the config.
`reconcile-once` and a running service each hold their own copy of the guard state.

**Decisions from 2026-11-06 (D13):** S3 already builds the batch store, `!confirm <id>`, `!pending`
and the 72 h expiry. This session only adds layer 3: 3 or more irreversible actions (deactivate,
erase) in one pass wait for confirmation; 1 or 2 run as today.

**Goal:** deactivate and erase above a small number per pass wait for a human.

**Steps:**
- Persist pending irreversible actions with a stable batch id. Post a summary to the admin room
  (`N users would be deactivated: ...`).
- `!confirm <batch id>` from an allowlisted admin executes the batch; it expires after a configurable
  time. `!pending` lists open batches. Reuse the existing command router and replay protection.
- Below the threshold, actions run without confirmation as today.

**Tests:** unit tests for the command and the expiry; one integration test for the happy path.

---

## S6 Room identity and unified room provisioning (D3, D20)

**Goal:** the bot recognises its rooms by creator and marker, not by alias, and creates every room in
one place.

**Findings:** REC-04, SEC-02, REC-13, ONB-17, ONB-16, ONB-02.

**Handover from wave 1:** S2 added `room_version` and `creator` to `MatrixRoom` and caches create-event
info per room id. The onbot state marker in rooms is still editable by moderators, so the mapping must
not trust it alone. S13 left the internal Authentik URL that is written into room state to this session.

**Steps:**
- One room provisioning helper used by group rooms, lobbies, the space, the control room and notice
  boards. It writes the onbot marker in `initial_state` of `createRoom` (not afterwards), so a failure
  after creation can never produce an unmarked room (ONB-02, ONB-16).
- Identity by marker plus creator (D20), no separate mapping store: a room belongs to a group (or is
  the lobby, space or control room) when the bot is its creator (room details admin API, works
  without membership) and it carries the onbot marker of the expected type with that `group_pk`. The
  alias is presentation only. Alias conflicts and two rooms claiming the same group are reported in
  the log and admin room and that group is skipped, never adopted or guessed (REC-13, SEC-02).
- The provisioning helper raises the required power level for the onbot marker event types above any
  human level (v12: creator only), so a moderator cannot rewrite `group_pk`.
- Before `createRoom`, resolve the alias in the directory. If it points at a room that is not ours,
  report and skip the group instead of failing with `M_ROOM_IN_USE` or building a second room.
- One-time migration: adopt existing rooms by alias only if they carry the onbot marker and were
  created by the bot; afterwards never look up by alias again. If legacy rooms were created by a
  different account (legacy bot MXID), stop and report the question instead of widening the rule.
- Tombstones (D20): if a managed room has `m.room.tombstone`, follow `replacement_room` only when its
  `m.room.create` names the old room as `predecessor` and the bot is its creator; then stamp the
  marker there. Otherwise (a human upgraded the room) report it and skip the group. The bot does not
  upgrade rooms itself in this session (see S17).
- ONB-17: the control room is found the same way (creator plus `admin_room` marker).

**Tests:** the live scenarios from `08` check 4 (squat with a private and a public room, moderator
clears the alias) as integration tests, plus: moderator changes the main address, moderator tries to
edit the marker, a room upgraded by a human (reported, skipped).

**Size note:** this is the largest session. If it runs long, split after the provisioning helper.

---

## S7 Room repair

**From S5:** no integration test covers room teardown (block, kick) and deletion
(`delete_disabled_rooms`, now its own irreversible guard step that only runs in a pass that also
closes the room) against the live stack; the room path is covered by engine unit tests only. Add
live tests for teardown, deletion below the layer 3 limit, and deletion held for `!confirm`.

**Goal:** rooms converge back to the desired shape, not only at creation.

**Findings:** REC-09, REC-10, REC-11.

**Steps:** repair the space child/parent link and the marker on every pass (cheap check first);
close and detach a lobby when the lobby feature is switched off, and stop reverting a manual close
(D16: keep it closed, log and report once, an admin command withdraws the manual close); never unblock a room a human server admin blocked (record who blocked).

---

## S8 Onboarding correctness

**Goal:** exactly one notice board per mapped user, and nobody else gets one.

**Findings:** RUN-01, ONB-01, SEC-06, ONB-13, ONB-06, ONB-07, ONB-08, ONB-09, ONB-18, RUN-08.

**Steps:**
- RUN-01 / ONB-01: serialise every `m.direct` read-modify-write behind one lock (or keep the mapping in
  the bot's own store and write `m.direct` from it).
- SEC-06 / ONB-13: welcome only MXIDs that the last reconcile mapped; filter broadcast targets the same
  way; prune `m.direct` entries of users no longer mapped; do not replay every member on startup.
- ONB-06: create the control room and notice boards with explicit unencrypted state where intended, and
  detect an encrypted control room (log an error, post a plain notice if possible).
- ONB-07, ONB-08: handle users who left the notice board (re-invite or report, and do not count it as
  reached); stop retrying an unreadable DM every tick.
- ONB-09: use a deterministic `txnId` per (user, message) so a retry is deduplicated by Synapse.
- ONB-18: optional stable `id` per welcome message; normalise whitespace before hashing; say in the
  field description that an edit re-sends.
- RUN-08: decouple onboarding from the reconcile pass (fire and forget with bounded concurrency, or a
  queue), so a slow welcome cannot stall reconciliation.

**Tests:** the live check 12 scenario (20 concurrent welcomes) and check 6 (outsider joins) as
integration tests.

---

## S9 User identity across renames (D8)

**Goal:** a renamed Authentik user keeps their Matrix account and all its rooms and power levels.

**Findings:** SEC-07, MNT-13, SEC-08.

**Handover from S1:** a `KnownUsers` record already exists in bot account data
`<reversed domain>.onbot.known_users` as `users[pk] = {mxid, first_seen}`; ledger entries carry
`authentik_pk`. Bindings are never rewritten and duplicates are reported, not recorded. Build the D8
binding on top of it instead of adding a second store.

**Steps:**
- Confirm the design in D8 with the maintainer first, and check that the MAS admin API can resolve an
  upstream OAuth link by subject.
- One function resolves Authentik user to MXID (MNT-13). With `mas_admin`, resolve through MAS; otherwise
  use the persisted binding from S1. Report binding changes; fail closed on duplicate MXIDs.
- SEC-08: URL-encode every path segment in all clients.

**Tests:** rename a user in the integration stack and assert nothing is kicked, locked or re-invited.

---

## S10 Runtime lifecycle

**Goal:** the process recovers from upstream outages by itself, stops cleanly, and tells Docker when
it is unhealthy.

**Findings:** RUN-02, RUN-03, ONB-14, RUN-04, SUP-09, RUN-11, RUN-09, SUP-10, RUN-12, RUN-13.

**Steps:**
- RUN-02: retry startup steps (device registration, admin room, rate-limit override, avatar) in the
  background until they succeed, or make the reconcile loop re-run them; a failed device registration
  must not leave welcome sends broken forever.
- RUN-03 / ONB-14: on `M_UNKNOWN_POS` reset the position and start a fresh connection.
- RUN-04 / SUP-09 / RUN-11: a heartbeat (file or in-process timestamp) written by the reconcile loop and
  the sync pump; the Docker healthcheck reads it instead of minting MAS tokens and probing upstreams
  every 60 s. Keep the upstream probe as an explicit `onbot healthcheck --deep`.
- RUN-09 / SUP-10: install SIGTERM handling before startup work, cancel tasks with a bounded grace
  period; consider `tini` or `--init` in the Dockerfile. Target: `docker stop` exits 0 within 10 s.
- RUN-12: build clients inside an `AsyncExitStack`. RUN-13: one-shot commands skip the side effects
  they do not need.

**Tests:** the live check 10 (cold start) and check 11 (SIGTERM) as integration tests where feasible.

---

## S11 HTTP layer

**From the wave 1 review:** the rest of LIF-10: `healthcheck.py` (deep check) still passes when MAS
answers not-found for the bot's own user; require a 200 there. The `synapse_server.oauth2` path is
still untested end to end (S10 handover). The SSRF guard in `safe_fetch.py` replaces httpx's private
`_pool`; keep its unit tests running on every httpx update and consider declaring `httpcore`.

**Goal:** retries never duplicate effects, and auth refresh is safe under concurrency.

**Findings:** RUN-05, RUN-06, SEC-10, RUN-10, RUN-14, RUN-15, RUN-16.

**Steps:** retry only idempotent requests, or make them idempotent (`txnId` for sends, a client-side
marker check before re-creating a room); single-flight token refresh with 401 invalidation and MAS 5xx
retry; honour `retry_after_ms`; per-call timeouts; keyset or re-check pagination where Authentik offers
it; httpx logs at WARNING; delete the dead version gating.

---

## S12 Broadcast and admin room

**Handover from S8:** `BroadcastService` requires a `recipients` source (the reconciler's mapped
set); `RecipientsUnknown` and `BroadcastResult.refused` / `not_member` are the new outcomes;
`summary()` keeps the "sent to N rooms, F failed" prefix plus ", K skipped (...)". Pacing and
truncation can build on the per-room `_send`, which does one membership GET before each send. The
integration helper `S.recipients(*mxids)` stands in for the mapped set. A pre-existing em dash in the
`!status` output string is S14's.

**Findings:** ONB-03, ONB-04, ONB-10, ONB-11, ONB-12, SEC-09, SEC-12, ONB-15.

**Steps:** pace the broadcast by the server's rate-limit answers; truncate the `!announce` result
reply and put the full failure list in a follow-up or the log; send announcements with
`formatted_body` through `formatting.py`; ignore edits (`m.replace`) entirely; stop re-inviting an
admin who left (remember the leave); re-check admin group membership locally; record handled event ids
only after the allowlist check; key avatars by content hash as well as URL (ONB-15).

---

## S13 Security hardening and config validation

**Findings:** SEC-03, RUN-07, SEC-11, MNT-03, SEC-05 (docs only, D6).

**Steps:**
- SEC-03: avatar fetch with a size cap, total timeout, no redirects (or redirects re-checked), and a
  block on private, loopback and link-local addresses unless explicitly allowed in config.
- RUN-07 / SEC-11: catch `ValidationError` in the CLI and print field paths without input values;
  switch secret fields to `SecretStr`; stop writing the internal Authentik URL into room state.
- MNT-03: `extra="forbid"` on nested models, exactly-one bot credential, URL scheme required, MXID and
  alias validators, `server_tick_rate_sec > 0`.
- SEC-05: document the concrete consequences of `chatroom_params` in `docs/authentik-groups.md` (D6).

---

## S14 Docs, dead code, cleanup

**From S7:** upkeep costs about 4 extra GETs per room per pass (REC-16); the control room
(`onbot/rooms/admin.py`) gets no marker, alias or lock repair yet; stale space links to an old space
are not removed after the space is recreated; a closed lobby keeps the levels the bot granted there;
the integration teardown tests delete other tests' group rooms on the shared stack (harmless today).

**From S12:** the first start after the upgrade rewrites every onbot marker once ("marker was
incomplete", info log) and downloads each older notice board's avatar once; the broadcast stall
limit (300 s), the wait cap (60 s) and the 20-room listing are constants; pre-existing em dashes in
the `!status` line and the help text. Not done, would need a decision: re-fetching an avatar URL
without a restart (ETag or periodic check), the SEC-09 local re-check for
`sync_only_users_of_groups_with_id`, the ONB-11 duplicate guard for identical announcements.

**From D17:** speed up the integration suite (about 20 minutes in CI): cache or reuse the stack start
(Authentik dominates), run independent test files in parallel where the stack allows it.

**Findings:** MNT-12, SUP-18, SUP-19, ONB-19, MNT-10, MNT-14, MNT-15, REC-15, ONB-05 (D5), LIF-15
leftovers, optionally REC-16 and REC-17.

**Steps:** bring `todo.md`, `issues.md`, `CHANGELOG.md` (releases 0.0.1 to 0.0.11 are published),
`BATTLE_PLAN.md` (close Phase 1 per D1 and D2), `architecture.md` links and `tests/README.md` in line
with the code; add the legacy cutover note (D5); delete dead code; fix the layering items that are
cheap; include all inputs in the discovery fingerprint. REC-16 (performance) and REC-17 (engine
restructure) are optional and only worth it once S2, S3 and S6 have settled the engine's shape.

---

## S15 CI and release hardening

**Findings:** SUP-02, SUP-04, SUP-05, SUP-06, SUP-07, SUP-08, SUP-12, SUP-13, SUP-14, SUP-15, SUP-16,
SUP-17.

**Steps (code):** release workflow requires the CI run of the tagged commit to be green; least-privilege
`permissions:`; actions pinned by SHA; CI checks out the PR head SHA, not `head_ref`; no auto-format
commits to `main`; one version source for image and wheel that handles PEP 440 tags; Dependabot or
Renovate for the base image, actions and Python deps; reproducible image build from `pdm.lock` with
hashes; upper bounds where needed; the provision script reads the token from stdin and offers to revoke
old ones; the test stack binds to 127.0.0.1; `build_dev_env.sh` does not leak `set -e`; the Dockerfile
copies only tracked package files.

**Maintainer actions (collect, do not perform):** protect `main` and tags, a `release` environment
holding the publish secrets, a Docker Hub token scoped to `dzdde/onbot`, PyPI trusted publishing,
yank `0.1.0.dev0`, decide about the stale `beta` tag.

---

## S16 Git history scrub (D2)

**Goal:** remove the two leaked tokens from history. They are already invalid (D1); this is hygiene.

**Steps:** follow the scrub plan in `02-supply-chain.md` (Maintainer actions). Prepare everything
locally on a fresh mirror clone: `git filter-repo` replacement rules for both tokens, verification with
gitleaks over the rewritten history, a mapping of old to new tag SHAs. Then hand the maintainer a
checklist for the irreversible part: force-push `main` and all 14 tags, re-create GitHub releases if
their tags moved, ask GitHub Support to purge cached views of the old commits, tell everyone with a
clone to re-clone. Nothing is pushed by Claude.

---

## S17 Bot-driven room upgrades (D20, deferred)

**Status:** later iteration, not before the next release. Not part of the wave plan.

**Goal:** an admin can move managed rooms to a new room version without members losing access or
history, and without the bot losing track of its rooms.

**Background:** Matrix has no real migration. An upgrade creates a new room id, tombstones the old
room, and moves the local aliases. Members are not moved, custom state (the onbot marker, space
links) is not copied, and encrypted history stays in the old room. New room versions are rare
(v10 2022, v11 2023, v12 2025) and the bot creates new rooms with the server default, so this is an
occasional maintenance action, not continuous reconciliation. The main candidates are pre-v12 rooms
adopted from the legacy bot (v12 state resolution fixes; REC-05 is already handled by S2).

**Steps:**
- Control room command, for example `!upgrade <group>` and `!upgrade legacy` (all managed rooms below
  a given version). Allowlisted admins only. Check the target against `/capabilities`
  (`m.room_versions`). A config default for the target version is fine, but no automatic upgrades in
  the reconciler.
- The bot calls `POST /rooms/{roomId}/upgrade`, so it is creator of the new room. Then: stamp the
  onbot marker, relink `m.space.child` / `m.space.parent`, and force-join every current group member
  through the admin join API so nobody has to follow the "room replaced" banner.
- The old room stays as a read-only archive (history remains readable for previous members). The bot
  keeps it in scope for offboarding: people who leave the group are removed from every predecessor in
  the tombstone chain. It adds nobody new there and removes it from the space.
- Report every upgrade in the admin room. Larger batches go through the D4 guard (count per pass,
  or a `!confirm` batch).

**Open before starting:** whether the group rooms federate (decides how much the v12 fixes matter for
legacy rooms), and whether old rooms should eventually be closed instead of archived.

**Note:** commit SHAs quoted in this review directory change after the scrub.
