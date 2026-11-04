# Handover: review fix work, state at end of 2026-11-04

Start a fresh session with: "Read docs/review_26-11-04/HANDOVER.md and continue the review fix work."
This file is the entry point; `SESSIONS.md` holds the session briefs and the status table,
`DECISIONS.md` the maintainer decisions (D1 to D12), `SUMMARY.md` the consolidated findings.

## Where things stand

Local `main` (not pushed) contains the review docs and five merged sessions:

| Session | Merge commit | Result |
|---|---|---|
| S0 Tooling baseline | e3282ef | pdm only, gitleaks in CI, pre-commit works, hermetic tests, parallel integration stacks (`ONBOT_ITEST_INSTANCE`) |
| S10 Runtime lifecycle | 9db28b1 | startup steps retry, sync pump recovers from `M_UNKNOWN_POS`, heartbeat healthcheck (`--deep` for upstreams), tini, clean `docker stop` |
| S1 Offboarding fails open | ab73206 | never unlocks unless positively active and in scope, deleted users offboarded, out-of-scope frozen (D9), per-user ledger save |
| S13 Security hardening and config validation | b063fe4 | strict config validation, `SecretStr`, config errors without values (exit 2), SSRF-safe avatar fetch (D11) |
| S2 Reconciler pass robustness | 44cdf95 | per-room isolation, lifecycle always runs, max PL 99 in pre-v12 rooms, D10 withdrawal of bot-granted levels, `enabled: false` no longer kicks |

After the S2 merge: 595 unit and contract tests green, all pre-commit hooks green. CHANGELOG
(Unreleased) carries the lines for S0, S10, S1 and S2. The S2 merge needed two conflict resolutions in
`engine.py` and `events.py` (both sides kept) and two test assertions adapted to S1's
`DirectorySnapshot` interface (`tests/unit/test_engine_robustness.py`).

**Integration status.** The first full integration run on main after merging S1, S2 and S10 had 14
failures, all caused by Synapse answering the bot's `createRoom` with HTTP 429: S10 (RUN-13) had removed
the rate-limit override from `reconcile-once`, and the integration tests call `run_reconcile_once` many
times. Fixed in a637961 (the override is a `reconcile-once` startup step again; it is also needed by
cron-driven deployments). A rerun of the six affected test files passed (22 passed). S13 was merged
afterwards: 750 unit and contract tests green, all hooks green, config docs regenerated. **A full
integration run on main after the S13 merge has not been done yet**; do it first tomorrow
(`ONBOT_ITEST_INSTANCE=0 ./run_integration_tests.sh`, about 30 minutes).

**Do not release yet.** Since S1 a known user who vanishes from Authentik is treated as deleted.
Until S3 adds the drop threshold, an Authentik token that suddenly sees only a few users would make
every other known user look deleted and lock them after the cooldown. Only an empty list is caught.

## Sessions that were still running at handover

One session agent of wave 1 (S15) was still running at handover and left to finish on their own. Each
works in a git worktree under `.claude/worktrees/` on its own branch, based on local `main` at the time
it started (`.claude/` is excluded locally via `.git/info/exclude`).

| Session | Branch | Stack instance | Started from |
|---|---|---|---|
| S15 CI and release hardening | `review/s15-ci-release` | 5 | 23cf1e9 |

If the previous session merged any of them before closing, the status table in `SESSIONS.md` says
"done" for it and `git log main` shows a `Merge review/...` commit. Otherwise do the following.

### 1. Check whether each branch is finished

The agents' final reports are gone with the old session. Reconstruct from git:

```bash
git log --oneline main..review/s15-ci-release
git -C .claude/worktrees/<agent-dir> status --short   # uncommitted work means the agent did not finish
```

A branch is finished when its commits cover the "Must hold" / "Done when" points of its brief in
`SESSIONS.md` and the worktree is clean. If a branch is unfinished, either continue it in a new
session agent (same branch, same worktree or a fresh one from the branch) or finish it by hand.
Leftover integration stacks: `docker ps --filter name=onbot-itest` and tear down with
`docker compose -f tests/integration/stack/docker-compose.yml -p onbot-itest-<n> down -v`.

### 2. Review and merge, one at a time

Only S15 is left. It must not touch `onbot/` Python code, so conflicts should be limited to the
Dockerfile (S10 added tini and the heartbeat healthcheck) and docs.

For each branch:

1. Read the diff (`git diff main...<branch>`), check it against the session brief and the findings it
   claims to fix.
2. `git merge --no-ff <branch> -m "Merge <branch>: <one line> (review session <Sx>)"`. Resolve
   conflicts keeping both sides' intent (so far conflicts were only doc tables).
3. `pdm run pytest tests/unit tests/contract -q` and `pdm run pre-commit run --all-files`.
4. After S13 the config-docs check fails until the docs are regenerated: run
   `pdm run gen-config-docs`, check the diff of `docs/CONFIG_REFERENCE.md` and `config.example.yml`,
   and confirm `config.example.yml` still validates against the stricter model.
5. Write the CHANGELOG lines for the session (Unreleased; destructive behaviour changes under
   Changed or Security, breaking config validation under Changed). Derive them from the commits and
   the brief, since the agent reports are not available.
6. Mark the session done in the `SESSIONS.md` status table and add handover notes to later sessions
   where the branch left something for them.
7. Commit. Session agents never edit CHANGELOG, generated config docs or the status table; that is
   the orchestrator's job.

After the whole wave: run the integration suite once on main
(`ONBOT_ITEST_INSTANCE=0 ./run_integration_tests.sh`), then start a fresh reviewer agent that reads
the wave's merged diff cold (without your summary) and checks every listed finding is really fixed.

Clean-up: `git worktree remove <path>` for finished worktrees (they may be locked by the old agent
process; `git worktree unlock <path>` first), delete the `worktree-agent-*` helper branches, keep the
`review/*` branches until the maintainer has pushed.

## Known handover items from finished sessions

- S1 emits `Signal.lifecycle_attention` (frozen users, failures, aborts) and logs `FROZEN`, `FAILED`,
  `lifecycle step aborted` at WARNING. No admin-room subscriber yet; S3 adds it (noted in its brief).
- S1 stores `KnownUsers` in bot account data `<reversed domain>.onbot.known_users`; S9 builds the D8
  identity binding on it (noted in its brief).
- S1 shaped the effectors as `erase(mxid, gdpr_erase=...)` plus `delete_media`; S4 implements D7 on it.
- S10: tini is installed via apt unpinned; S15 was told to pin it. The `synapse_server.oauth2` path is
  still untested end to end (S11). Replays after a sync reset still welcome non-mapped joiners (S8).
- S10: the heartbeat staleness thresholds (3 ticks plus 300 s for reconcile, 300 s for sync) are fixed,
  not configurable; a long Authentik outage marks the container unhealthy. Mention to the maintainer if
  their orchestrator restarts unhealthy containers.
- S13 did not touch the internal Authentik URL that is written into room state and into the kick
  reason; S6 handles it.
- S13 added `onbot/safe_fetch.py` and the `avatar_fetch` config block; `scripts/gen_config_docs.py` now
  builds psyplus' placeholder instance without validation.

## What comes next

Per the wave plan in `SESSIONS.md` (max 3 agents in parallel, one worktree and one stack instance per
agent, branches from local `main`):

1. Full integration run on main. Then finish wave 1 (merge S15 as above) and run the wave review over
   S1, S2, S10, S13, S15.
2. S16 history scrub, only while no `review/*` branch is open. Prepare locally on a mirror clone;
   the force-push is the maintainer's step.
3. Wave 2: S3 (guard layers 1 and 2, admin-room reporting), S4 (offboarding completeness, D7), S11
   (HTTP layer). S3 first if only one slot: it closes the release blocker above.
4. Waves 3 to 5 as listed.

Open maintainer decisions that later sessions will need (ask before starting the session):

- S13 (already merged, confirm or change): two config combinations that do nothing are now hard
  errors at startup, like the existing lobby rule: `delete_disabled_rooms` while rooms are not disabled,
  and onboarding rooms placed in a space while no space is configured. Alternative: warnings only.
- S2 / S14: the config descriptions of `sync_authentik_users_with_matrix_rooms.enabled` and
  `global_room_admins` (cap at 99 in pre-v12 rooms) still describe the old behaviour.

- S3: default thresholds for the input drop percentage and the per-pass destructive action limit.
- S5: threshold for irreversible actions that need `!confirm`, and the batch expiry time.
- S7: whether a manually closed lobby should stay closed.
- S9: confirm the D8 design once it is known whether the MAS admin API resolves upstream links by
  subject.
- S15: possibly the PyPI trusted publishing switch-over timing (if the S15 branch left it open).

## Working rules (maintainer preferences)

- Everything stays local: no push, no GitHub writes, no Docker Hub or PyPI actions. The maintainer
  pushes. Local commits on `review/*` branches and local merges into `main` are approved.
- No AI attribution in commits, no em or en dashes anywhere.
- Never open `config.dev.yml` or other untracked `config*.yml` (real credentials).
- Ask decisions with AskUserQuestion, one batch at a time, in German; repo docs stay in English.
- No claude.ai artifacts or hosted docs.
