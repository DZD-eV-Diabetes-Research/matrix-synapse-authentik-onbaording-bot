# Handover: review fix work, state at end of 2026-11-06

Start a fresh session with: "Read docs/review_26-11-04/HANDOVER.md and continue the review fix work."
This file is the entry point. `SESSIONS.md` holds the session briefs and the status table,
`DECISIONS.md` the maintainer decisions (D1 to D21), `SUMMARY.md` the consolidated findings, the area
files `01` to `08` the detailed write-ups.

**History note:** the git history was rewritten on 2026-11-06 (S16). Every commit SHA quoted in this
directory from before that day refers to the old history and no longer resolves.

## Where things stand

Local `main` is ahead of `origin/main` (the maintainer pushes; last push was the scrubbed history at
89afecf). Waves 0 to 3 are merged and verified; wave 4 is in progress.

| Wave | Sessions | Result | Full integration on main |
|---|---|---|---|
| 0 | S0 | tooling baseline | |
| 1 | S1, S2, S10, S13, S15, W1F | offboarding fails closed, pass robustness, runtime lifecycle, config validation, CI and release hardening, wave 1 review fixups (D14) | 855 passed |
| between | S16, CI1 | history scrub (force-pushed by the maintainer), shorter CI (D17) | first GitHub CI run green |
| 2 | S11, S3, S4, W2F, W2R | HTTP layer, guard layers 1 and 2 with `!confirm <id>`, offboarding completeness (D7), D19 fixups (MAS 1.28 and Synapse 1.164 in the test stack, admin room on by default), wave 2 review fixups | 1130 passed |
| 3 | S5, S6, S9, W3R | guard layer 3, room identity by creator plus locked marker (D20), user identity across renames (D8, D21), wave 3 review fixups | 1277 passed (before W3R; W3R's own full run on its branch: 1295 passed) |
| 4 and 5 | S8, S7, S12, W4R, S18, W6R, S14, W8R (merged) | onboarding correctness, room repair, broadcast and admin room, wave 4 review fixups | after S7 and S8: 1385 passed (a47c0f1); after S12 and W4R: 1454 passed (5deb135); S18 branch, code equal to main at b0aec61: 1502 passed; after W6R and S14 part 1 (3ffd05c, 2 workers): 1543 passed; S14 part 2 branch, code equal to main at c5bd4cb: 1569 passed; W8R branch, code equal to main: 1585 passed |

Status per session: the table in `SESSIONS.md`. Release notes: `CHANGELOG.md` (Unreleased) has a line
for every merged session.

**Release readiness:** no known path left that mass-kicks, mass-locks or wrongly unlocks or erases.
All critical findings and 8 of 10 high findings are fixed. The two remaining high findings are
REC-04 (fixed by S6 since) and RUN-01/ONB-01 (lost `m.direct` entries, S8). After wave 4 nothing
blocks a release from a safety point of view.

## Wave 4 (in progress)

- **S8 Onboarding correctness: merged.** All 10 findings fixed (RUN-01, ONB-01, SEC-06, ONB-13,
  ONB-06, ONB-07, ONB-08, ONB-09, ONB-18, RUN-08); its full integration run on the branch: 1321
  passed. After the merge: 1274 unit and contract tests green, config docs regenerated. Leftover:
  the `m.direct` prune only drops deactivated or remote accounts; local active accounts an older
  version enrolled stay (excluded from broadcasts, not pruned).
- **S7 Room repair: merged** (no conflicts with S8). REC-09, REC-10 (D16 manual lobby close and
  `!lobby reopen`), REC-11 fixed, live teardown and deletion tests added, plus a found bug: a closed
  room was torn down again every pass (from 11 closed rooms on, every kick waited for a confirmation).
  Its full run on a fresh stack: 1337 passed. After the merge: 1307 unit and contract tests green,
  config docs regenerated. Leftovers moved to the S14 brief.
- **Full integration run on main after S8 and S7 (717ce6c): 1384 passed, 1 failed.** The failure was
  an S7/S8 interaction in a test only: S7's lobby test built `BroadcastService` without the
  `recipients` argument S8 made required. Fixed in the next commit, `test_room_repair.py` rerun: 9
  passed. The code itself passed: the guard held the three deletions as intended.
- **S12 Broadcast and admin room**: not started (moved from wave 3 to wave 4 because it builds on S8's
  broadcast target filtering). Start it after S8 is merged, with S8's handover notes.
- If an S7 or S8 branch is still unmerged when a new session starts: check `git log main..<branch>`,
  read the agent's report if it is in this file below, otherwise review the branch against its brief,
  then merge per "How merging works".

## Next steps (session of 2026-11-09, in progress)

Done this session:

- Full integration run on main at a47c0f1: 1385 passed.
- Cold review of wave 4 (8 findings, no critical) turned into W4R; decisions D22, D23, D24.
- S12 merged (15c772b, its full run on the branch: 1432 passed), W4R merged (5deb135, its full run:
  1405 passed, 1 flaky HTTP-layer test now in S18). The W4R merge had conflicts with S12 in
  `welcome.py` and `engine.py`: S12's avatar marker writes now go through W4R's `_put_marker`.
  Unit and contract after both: 1369 passed.

Also done: full integration run on main at 5deb135 (S12 and W4R together): 1454 passed, 22 minutes.
Cold review of S12 plus W4R: 5 findings (2 medium, 3 low), no critical; added to S18 as W5R-01 to
W5R-05 (see the S18 brief).

S18 merged (b0aec61): the D24 follow-ups plus W5R-01 to W5R-05. Its full run on the branch, with code
identical to main: 1502 passed; the flaky deletion test passed 10 of 10 times. Unit and contract on
main: 1413 passed. D25 records the S18 confirmations.

Cold review of S18: 8 findings (1 medium, 7 low), no critical or high, written up as W6R in
`SESSIONS.md` (items from S14 that sit in the same files moved there). The production template is not
affected by W6R-01 (it uses hyphenated pks; the other filters are empty). S14 is split into part 1
(parallel to W6R) and part 2 (after W6R).

W6R merged (bf51958) and S14 part 1 merged (bfd073b). Full integration run on main at 3ffd05c with
the new parallel mode (`ONBOT_ITEST_WORKERS=2`): 1543 passed in 17:49 (serial was about 22 minutes).
Cold review of both: 1 medium (changelog upgrade warning for the pk forms, fixed by the orchestrator)
and 4 low findings, carried into S14 part 2.

S14 part 2 merged (c5bd4cb, code identical to its branch): full run on the branch 1569 passed (the 2
config docs drift tests, failing there by design, are green on main after regeneration); unit and
contract on main 1480 passed. Includes the D27 rename (breaking). Cold review of part 2: 1 medium
(a switched-off lobby blocked by someone else loses its levels, D23) and 7 low, written up as W8R.
D26 to D28 record the confirmations.

W8R merged (code identical to its branch at 8b68c8e): full run on the branch with 2 workers 1585
passed in 11:32 (2 config docs drift tests failing there by design, green on main after
regeneration); unit and contract on main 1494 passed.

The review run is complete. The last cold review (of W8R) found one low item (W9R-01, a retry lost
on restart), documented and listed in `SESSIONS.md` under "After the release". What remains: the
maintainer to-dos below (including the deployment template changes prepared uncommitted in the local
clone of `infra-node-conf-com` on 2026-11-09: `oauth2` removed, D27 rename, topics, container
hardening, `ONBOT_IMAGE` placeholder), S17 (deferred, not before the next release), REC-16 and the
REC-17 proposal (in the S14 part 2 report, summarised in `SESSIONS.md` if picked up later).

If this session stops before they finish: check `git branch` and `git worktree list` for
`review/w8r-*`, review it against its brief and merge per "How merging works"; otherwise start W8R from
its brief.

## Questions collected for the maintainer

All answered on 2026-11-09: recorded in D13 (guard details), D21 (binding wins without MAS, done in
W4R), D22 (S7 and S8 confirmations), D23 (rooms blocked by someone else are a hold, done in W4R) and
D24 (S12 and W4R confirmations, new work in S18) D25 (S18 confirmations), D26 (W6R confirmations), D27 (S14 confirmations) and D28 (S14 part 2 confirmations).

## Maintainer to-dos outside the code

- ~~Push `main`.~~ Done 2026-11-09: `main` pushed (79fd7c7), CI green (integration suite 15:46 on a
  GitHub runner with 2 workers), released as **0.1.0** (PyPI `onbot==0.1.0`, Docker Hub
  `dzdde/onbot:0.1.0`, `0.1`, `0`, `latest`).
- GitHub Support request to purge cached views of the old commits b16af39, 4a7488f, 6f7a00c and their
  descendants (old history).
- Repository settings from the S15 checklist in `docs/development.md`: protect `main` and tags
  (disallow force pushes now that the scrub is done), create the `release` environment with a
  reviewer and move the Docker Hub and PyPI secrets into it, Actions read-only by default, Docker Hub
  token limited to `dzdde/onbot`, PyPI Trusted Publishing then delete `PYPI_API_TOKEN`, yank
  `0.1.0.dev0`, decide on the stale `beta` image tag.
- Deployment template (`infra-node-conf-com`, `compose/11_matrix/templates/onbot-config.yml.template`):
  once S14 part 2 is merged, rename `deactivate_after_n_sec` to `lock_after_n_sec` and
  `delete_after_n_sec` to `remove_after_n_sec` (D27), otherwise the new version refuses to start.
- Deployment (not productive yet, `infra-node-conf-com`): it already uses Synapse's stable
  `matrix_authentication_service` block, which Synapse 1.164 requires; the Authentik provider for MAS
  uses the hashed user ID as `sub` (matches `mas_admin.upstream_subject_attribute: uid`). The onbot
  config there needs an admin in `admin_room.admin_user_ids` (or a group) now that the admin room is
  on by default, or `admin_room.enabled: false`.

## How merging works

For each finished branch:

1. Read `git diff main...<branch>` against the brief and the agent's report.
2. `git merge --no-ff <branch>`; resolve conflicts keeping both sides' intent. Recurring hot spots:
   `onbot/lifecycle/accounts.py`, `onbot/reconciler/engine.py`, `onbot/app.py`, `onbot/events.py`,
   test fakes (since S6, engine tests that replace `engine.admin` must also set
   `engine.rooms.admin` and `engine.rooms.writer`; managed rooms in fakes need markers via
   `tests/unit/room_fakes.py`).
3. `pdm run gen-config-docs` if the config model changed, `pdm run pytest tests/unit tests/contract -q`,
   `pdm run pre-commit run --all-files`.
4. CHANGELOG lines from the agent's report (behaviour changes of destructive features under Changed
   or Security), mark the session done in `SESSIONS.md`, carry leftovers into later briefs.
5. Commit **explicit paths only** (`git add <files>`), never `git commit -a`: the maintainer edits the
   review docs in parallel (D20, S17 and the revised S6 brief were once swept into an unrelated commit
   that way).
6. Remove the agent's worktree (`git worktree unlock`, `git worktree remove`) and delete its
   `worktree-agent-*` and `review/*` branches.

After each wave: one full integration run on main (`ONBOT_ITEST_INSTANCE=0 ./run_integration_tests.sh`,
about 13 minutes now) and a cold review by a fresh agent. Both have caught real problems in every wave
(wave 1: an S1 regression; wave 2: the guard failed open on an unreadable state; wave 3: an attribute
guess could take a MAS-linked account).

## How session agents are run

- Max 3 in parallel, each with `isolation: worktree`, its own branch `review/<session>-<slug>`
  created explicitly from a given `main` commit (worktrees may start from an old commit), and its own
  stack instance (`ONBOT_ITEST_INSTANCE=1..3`; 0 is the orchestrator's).
- Agents share the session scratchpad: give every log a unique file name.
- Every wait (orchestrator and agents) has a hard timeout and ends when the watched process is gone,
  for example `timeout 2400 bash -c 'while kill -0 $PID 2>/dev/null; do sleep 10; done'`, or relies
  on the background completion notification. Never a bare `until grep <log>` or `pgrep -f` loop (one
  ran for over 8 hours on 2026-11-06). Put this rule into every agent brief that runs tests, and run
  a report-only watchdog in the background while agents test (flags wait loops over 45 minutes or
  with a log unchanged for 15 minutes, and pytest runs over 45 minutes).
- Before briefing an agent, re-read the current brief and the decisions it depends on.
- Agents do not edit CHANGELOG, generated config docs, DECISIONS.md or the status table; they report
  product questions instead of deciding them.

## Working rules (maintainer preferences)

- Everything stays local: no push, no GitHub writes, no Docker Hub or PyPI actions. The maintainer
  pushes. Local commits on `review/*` branches and local merges into `main` are approved.
- No AI attribution in commits, no em or en dashes anywhere.
- Never open `config.dev.yml` or other untracked `config*.yml` (real credentials).
- Ask decisions with AskUserQuestion, one batch at a time, in German; repo docs stay in English.
- No claude.ai artifacts or hosted docs.
- Deployment repos (`configrepos.vault.dzd-ev.org/*`) may be read locally; never touch the server.
