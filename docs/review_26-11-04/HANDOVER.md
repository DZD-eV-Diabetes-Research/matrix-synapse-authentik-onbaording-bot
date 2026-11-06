# Handover: review fix work, state at end of 2026-11-04

Start a fresh session with: "Read docs/review_26-11-04/HANDOVER.md and continue the review fix work."
This file is the entry point; `SESSIONS.md` holds the session briefs and the status table,
`DECISIONS.md` the maintainer decisions (D1 to D12), `SUMMARY.md` the consolidated findings.

## Update 2026-11-06 (read this first)

- Worktrees and `worktree-agent-*` branches of wave 1 are removed; `review/*` branches are kept.
- Cold wave review of waves 0 and 1 done (two reviewers, S1/S2 and S0/S10/S13/S15). Everything listed
  is fixed except the items now in the new session **W1F** (see `SESSIONS.md`) and handover notes added
  to the S3, S4, S5 and S11 briefs. Most important find: D14 (known user disabled and out of scope at
  the same time was never offboarded, a regression from S1).
- Maintainer decisions D13 (guard confirmation with `!confirm <id>`, `!pending`, `!reject <id>`, 20 %,
  10 per pass, 3 irreversible, 72 h; S3 builds it), D14, D15 (confirmations, trixie, fixed healthcheck
  thresholds) and D16 (manually closed lobby) are recorded. D12 amended: no extra monitoring for
  Python dependencies.
- Full integration run on main (556c41b): 820 passed, 1 failed. The failure was a test written by S2
  that passed the S13 `SecretStr` token unwrapped (401); fixed in 0173aa6, file rerun green (4 passed).
- W1F merged (ea282bd, follow-up 8d2e642): 824 unit and contract tests green, config docs
  regenerated. Full integration run on main after W1F (2e885e3): 855 passed, 0 failed.
- S16 timing decided: the maintainer first pushes the current `main` normally so the new CI runs once
  on GitHub, then the scrub is prepared on a mirror clone and the maintainer force-pushes `main` and
  all tags. Wave 2 starts after the scrub (or is rebased onto the rewritten `main`).
- Next: merge W1F (CHANGELOG lines, regenerate config docs since descriptions change, status table),
  one full integration run on main, then S16 (no open branches) and wave 2 (S3, S4, S11).
- Still open for later: S9 (D8 design, once the MAS admin API behaviour is known). The S15
  maintainer checklist in `docs/development.md` is still the maintainer's to do.

**S16 done (2026-11-06).** The history was rewritten with `git filter-repo` (both `syt_` tokens
replaced by `REDACTED_TOKEN`, `onbot/test.py` and `onbot/readme_feat_room_avatar.txt` removed), verified
with gitleaks, and force-pushed by the maintainer: `main` 771cdaa became 89afecf, all 14 tags moved.
**Every commit SHA quoted in this directory from before 2026-11-06 refers to the old history and no
longer resolves.** The workstation clone was rebased onto the new `main`; all old `review/*` branches
are deleted. Still open for the maintainer: GitHub Support purge request (old commits b16af39,
4a7488f, 6f7a00c and descendants) and protection of `main` and tags (A5 in `02-supply-chain.md`).
The first real CI run on GitHub (old 771cdaa) was fully green, 23 minutes.

**CI1 done** (merge 4da9c49): `scripts/ci_gate.py`, path filter for the integration job, release
reuse of a green CI run, full-history secret scan. Verified with actionlint and against real API
data of a finished run; the reuse path itself runs for the first time at the next release.

**Wave 2 complete** (2026-11-06): S11, S3, S4, then the fixup sessions W2F (D19: test stack on MAS
1.28.0 and Synapse 1.164.2, offboarding refused under MAS without `mas_admin`, admin room on by
default with a required admin) and W2R (cold wave 2 review findings: guard fails closed on an
unreadable state with `!guard reset`, rejected batches end with their cause, unreadable ledger
reported again, demotions and retention guarded, async v2 room deletion). Merges needed manual
resolution in `onbot/lifecycle/accounts.py` (S4 and W2R), `onbot/app.py`, `onbot/events.py` and
`docs/troubleshooting.md`. Final full integration run on main (9a43e83): **1130 passed, 0 failed**,
10.5 minutes. The deployment repo already uses Synapse's stable `matrix_authentication_service`
block, which Synapse 1.164 requires.

**Wave 3 merged** (2026-11-06): S5 (guard layer 3, room deletion split from teardown, media
continuation without re-confirmation), S6 (room identity by creator plus locked marker per the
maintainer's D20, one provisioning path, squatting refused, SEC-11), S9 (identity anchored on the
Authentik user via MAS link or recorded binding, D21 conflicts, SEC-08 path encoding). The S6 merge
needed resolution in engine.py (teardown reason without the Authentik URL) and in S5's engine guard
tests (rooms registered as bot-created marked rooms, identity resolver rewired). S9 merged main into
its branch itself. After the merges: 1214 unit and contract tests green, hooks green, config docs
regenerated. Running: full integration on main (instance 0) and a cold wave 3 review.

Note: D20, S17 and the revised S6 brief were written by the maintainer and were swept into commit
44346fe by a `git commit -a`; from now on commit explicit paths only.

**Questions collected for the maintainer (ask with AskUserQuestion when back):**
- D21 gap: without MAS (or when MAS does not link the account), a recorded binding against a
  never-seen attribute guess currently fails closed for both users. Should the binding holder win
  (as the MAS-linked owner does)?
- Guard, implemented conservatively by W3R pending confirmation: (a) a rejected batch with
  irreversible actions keeps them held until it expires, instead of ending once fewer than 3 remain
  (the remaining 1 or 2 erasures would otherwise run without asking); (b) confirmed batches expire
  too (a deletion waiting weeks for its teardown is proposed again instead of running on an old
  confirmation). Confirm both or choose the original behaviour.

**Wave 3 review (cold):** S5 fixed; S6 fixed (REC-04, SEC-02, REC-13, ONB-17, ONB-16, ONB-02, SEC-11);
S9 SEC-07 partial. New findings handed to W3R (instance 1): an attribute guess can take an account MAS
links to an absent unbound owner; stray legacy rooms adoptable after a moderator rewrites the marker
(no marker sender check); a followed upgrade becomes a permanent conflict; failed marker lock cached as
locked. S8 started in parallel (instance 2); S7 and S12 follow after W3R and S8.

**Next:** act on the wave 3 review, then wave 4 (S7, S8, S12), then wave 5 (S14).

## Where things stand (2026-11-04)

Local `main` (not pushed) contains the review docs and all six sessions of waves 0 and 1:

| Session | Merge commit | Result |
|---|---|---|
| S0 Tooling baseline | e3282ef | pdm only, gitleaks in CI, pre-commit works, hermetic tests, parallel integration stacks (`ONBOT_ITEST_INSTANCE`) |
| S10 Runtime lifecycle | 9db28b1 | startup steps retry, sync pump recovers from `M_UNKNOWN_POS`, heartbeat healthcheck (`--deep` for upstreams), tini, clean `docker stop` |
| S1 Offboarding fails open | ab73206 | never unlocks unless positively active and in scope, deleted users offboarded, out-of-scope frozen (D9), per-user ledger save |
| S13 Security hardening and config validation | b063fe4 | strict config validation, `SecretStr`, config errors without values (exit 2), SSRF-safe avatar fetch (D11) |
| S15 CI and release hardening | 257df75 | releases gated on full CI from main, SHA-pinned actions, reproducible hash-checked image, one version source, Dependabot, hardened dev scripts |
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

## Wave 1 is complete

All wave 1 agents finished and their branches are merged. No session agent is running. The finished
worktrees under `.claude/worktrees/` can be removed (`git worktree unlock <path>` then
`git worktree remove <path>`), and the `worktree-agent-*` helper branches deleted. Keep the `review/*`
branches until the maintainer has pushed.

Before starting wave 2:

1. Run the full integration suite once on main: `ONBOT_ITEST_INSTANCE=0 ./run_integration_tests.sh`
   (about 30 minutes). It has not run since S13 and S15 were merged. If createRoom 429s show up again,
   check that `reconcile-once` still lifts the rate limit (a637961).
2. Wave review: start a fresh reviewer agent that reads the merged diff of waves 0 and 1
   (`git diff 1e91cd2..257df75`) cold, without the orchestrator's summary, and checks that every finding listed for S0, S1, S2, S10, S13 and S15 in
   `SESSIONS.md` is really fixed and nothing regressed.
3. Ask the maintainer the open questions below.

### How merging worked (for the next waves)

For each finished branch: read `git diff main...<branch>` against the brief, `git merge --no-ff`,
resolve conflicts keeping both sides' intent, `pdm run pytest tests/unit tests/contract -q`,
`pdm run pre-commit run --all-files`, regenerate config docs (`pdm run gen-config-docs`) if the model
changed, write the CHANGELOG lines from the agent's report, mark the session done in `SESSIONS.md`, add
handover notes to later session briefs, commit. Session agents never edit CHANGELOG, generated config
docs or the status table. After the wave, one full integration run on main.

Lesson from wave 1: branches that are green on their own can fail together (S10 removed a startup
step that tests of S1 and S2 depended on). The full integration run after each wave is not optional.

## Known handover items from finished sessions

- S1 emits `Signal.lifecycle_attention` (frozen users, failures, aborts) and logs `FROZEN`, `FAILED`,
  `lifecycle step aborted` at WARNING. No admin-room subscriber yet; S3 adds it (noted in its brief).
- S1 stores `KnownUsers` in bot account data `<reversed domain>.onbot.known_users`; S9 builds the D8
  identity binding on it (noted in its brief).
- S1 shaped the effectors as `erase(mxid, gdpr_erase=...)` plus `delete_media`; S4 implements D7 on it.
- S10: tini is now a checksummed static binary (S15). The `synapse_server.oauth2` path is
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

1. Full integration run on main and the wave review (see "Wave 1 is complete").
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
- S15 (merged, confirm): releases are only allowed from tags on `main`; releases take about 20
  minutes longer because the integration suite runs first.
- S15: Dependabot cannot update `pdm.lock`, and GitHub's dependency graph reads the stale `uv.lock`
  until main is pushed. Add a `pip-audit` CI job, or switch to Renovate (which supports pdm)?
- S15: stay on Debian bookworm for the base image or move to trixie?
- S15 maintainer checklist (in `docs/development.md`): protect `main` and tags; create the `release`
  environment with a reviewer and move the Docker Hub and PyPI secrets into it; set Actions to
  read-only by default; limit the Docker Hub token to `dzdde/onbot`; set up PyPI Trusted Publishing,
  then delete `PYPI_API_TOKEN`; yank `0.1.0.dev0`; delete or keep the stale `beta` image tag.

## Working rules (maintainer preferences)

- Everything stays local: no push, no GitHub writes, no Docker Hub or PyPI actions. The maintainer
  pushes. Local commits on `review/*` branches and local merges into `main` are approved.
- No AI attribution in commits, no em or en dashes anywhere.
- Never open `config.dev.yml` or other untracked `config*.yml` (real credentials).
- Ask decisions with AskUserQuestion, one batch at a time, in German; repo docs stay in English.
- No claude.ai artifacts or hosted docs.
