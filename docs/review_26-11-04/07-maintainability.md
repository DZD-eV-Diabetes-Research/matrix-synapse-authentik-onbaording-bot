# 07 Maintainability: config model, dead code, structure, tests, docs drift, tooling

Every config field in `onbot/config.py` is read somewhere, and the generated docs are in sync
(`scripts/gen_config_docs.py --check`: OK for both files). The bigger problems are config switches
that do something other than what their names and descriptions say. Turning off user sync empties
every room. Offboarding unlocks users who get deleted. Many invalid values are accepted without an
error. The suite has 377 green tests and 90% coverage, but the MAS lifecycle effectors (the
destructive production path) have no tests at all, and the unit tests read a developer's local
`config.yml`. Tooling has drifted too: there are two lockfiles that disagree, the pre-commit hooks
cannot run, and the planning docs (todo, issues, CHANGELOG, BATTLE_PLAN) are behind `git log`.

| ID | Severity | Title |
|---|---|---|
| MNT-01 | high | Disabling user sync (or a user filter that matches nobody) kicks every member from every group room |
| MNT-02 | high | Offboarding never sees deleted Authentik users and "re-enables" (MAS-unlocks) tracked users who are deleted or leave scope |
| MNT-03 | medium | Config accepts many values that are wrong or break at runtime |
| MNT-04 | medium | Lifecycle and room-setting descriptions contradict the code |
| MNT-05 | medium | Destructive paths with no test in any tier, MAS lifecycle effectors untested |
| MNT-06 | medium | Unit tests are not hermetic: a local `config.yml` or `ONBOT_*` env changes results |
| MNT-07 | medium | Two lockfiles that resolve different versions; pdm is canonical, `uv.lock` is stray |
| MNT-08 | medium | Pre-commit hooks cannot run: pinned ruff 0.8.4 rejects the project config |
| MNT-09 | medium | The reconciler has no dry-run; `DryRunEffectors` is dead in production and its docstring says otherwise |
| MNT-10 | low | Dead code and dead API surface in `onbot/` |
| MNT-11 | low | The documented `onbot reconcile-once --log-level DEBUG` fails with an argparse error |
| MNT-12 | low | Planning and docs drift: todo.md, issues.md, CHANGELOG, BATTLE_PLAN, architecture.md, tests/README, stale code comments |
| MNT-13 | low | Authentik user to MXID resolution is implemented four times with drifting rules |
| MNT-14 | low | Module layering: shared state kernel lives in `reconciler/`, one-module `rooms/` package, healthcheck imports the composition root |
| MNT-15 | low | Tooling leftovers: committed Black editor config, four config-template entry points, coverage gate split no longer justified |

`legacy/`: nothing outside `legacy/` imports it (checked `grep -rn "legacy" onbot`: only docstrings
saying "ported from legacy"). `ruff` and `mypy` exclude it.

---

## High

### MNT-01 Disabling user sync (or a user filter that matches nobody) kicks every member from every group room

- **Severity:** high
- **Category:** bug
- **Location:** onbot/reconciler/engine.py:156-159, onbot/reconciler/engine.py:433-468, onbot/config.py:476-482, onbot/config.py:497-508
- **Status:** confirmed (scratch script reusing the `tests/unit/test_engine.py` fakes with `sync_authentik_users_with_matrix_rooms: {enabled: false}`: `kicks == [('!room1', '@alice'), ('!room1', '@stale')]`)
- **Effort:** S

`sync_authentik_users_with_matrix_rooms.enabled` is described as the "Master switch for projecting
Authentik group membership into Matrix rooms". An operator who sets it to `false` expects the bot to
stop touching membership. The code does something else. `_gather_mapped_users` returns `[]`, but room
sync (`sync_matrix_rooms_based_on_authentik_groups.enabled`) is still on, so
`_converge_room_membership_and_levels` still runs for every group room. Its desired member set is now
just the global admins, and `kick_matrix_room_members_not_in_mapped_authentik_group_anymore` defaults
to `true`, so every member of every managed room gets kicked on the next tick. Power levels are
withdrawn the same way.

The same thing happens without touching the switch, through any user filter that matches nobody: a
typo in `sync_only_users_in_authentik_pathes` (`["user"]` instead of `["users"]`), a
`sync_only_users_with_authentik_attributes` value nobody carries yet, or a wrong
`sync_only_users_of_groups_with_id`. Nothing guards against "zero users this pass, N members last
pass". G12.2 in GOALS ("every destructive or sweeping behaviour is opt-in") does not hold, because
the kick is on by default.

Fix sketch: (1) when user sync is disabled, skip the membership and power-level pass entirely (or
reject `users.enabled=false` together with `rooms.enabled=true` in a model validator, if the
combination makes no sense); (2) add a circuit breaker: when a pass would kick more than X% of a
room's members, or when the mapped user set is empty while Matrix has users, log loudly and skip the
kicks for that pass; (3) say in the `kick_...` description that it is destructive and on by default.
Overlaps the reconciler area (03); keep one copy in SUMMARY.

### MNT-02 Offboarding never sees deleted Authentik users and "re-enables" (MAS-unlocks) tracked users who are deleted or leave scope

- **Severity:** high
- **Category:** bug
- **Location:** onbot/reconciler/engine.py:629-663, onbot/lifecycle/accounts.py:236-271, onbot/config.py:411-421, docs/features.md:411
- **Status:** confirmed (code reading; `decide_account_action(is_orphaned=False, marked_ts=0, disabled_ts=100, ...)` returns `reenable`)
- **Effort:** M

The config description of `deactivate_disabled_authentik_users_in_matrix.enabled`, the module
docstring of `lifecycle/accounts.py`, and `docs/features.md` all say offboarding covers users who
are "disabled **or deleted**" in Authentik. `_gather_orphaned_mxids` only queries
`list_users(filter_is_active=False)`, so a deleted user never becomes an orphan.

It gets worse once a user is already in the ledger. `reconcile_accounts` iterates
`orphaned_mxids | set(ledger.entries)`. Any tracked user who is not an orphan in this pass is passed
to `decide_account_action(is_orphaned=False, ...)`, which returns `reenable`, and `_apply` calls
`effectors.reenable` even under `dry_run` (MAS: `unlock_user`) and drops the ledger entry. A tracked
user stops being an orphan when they:

- are deleted in Authentik (the usual end of "disable, then delete later"),
- are added to `authentik_user_ignore_list` or `matrix_user_ignore_list`,
- stop matching `sync_only_users_with_authentik_attributes`,
- have their mapping attribute removed (`compute_mxid` raises `KeyError`).

In all four cases the locked account is unlocked, and the 365-day erase never happens because the
ledger entry is gone. The data-protection promise in `include_user_media_on_delete` and
`delete_after_n_sec` silently fails for exactly the users who were removed most thoroughly.

Fix sketch: only re-enable when the user is positively seen as **active** in Authentik (pass the
active MXID set, which the engine already has as `active_mxids`, and require membership in it).
Treat "tracked but not seen at all" as still orphaned. Add the deleted case by diffing tracked
MXIDs against all Authentik users. Fix the docs if deleted users are meant to be out of scope.
Overlaps 04 (lifecycle); keep one copy in SUMMARY.

---

## Medium

### MNT-03 Config accepts many values that are wrong or break at runtime

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/config.py (whole model; only one `model_validator` at 1336), onbot/reconciler/engine.py:118-121, onbot/identity.py:21-31, onbot/app.py:49-61
- **Status:** confirmed (scratch script validating each case below against `OnbotConfig.model_validate`: all accepted except top-level unknown keys and `log_level: WARNING`)
- **Effort:** M

All of these are accepted at load time:

1. **Typos in nested blocks are silently ignored.** The sub-models are plain `BaseModel` (default
   `extra="ignore"`). Only top-level keys are rejected, because `BaseSettings` forbids extras.
   `dry_rn: false`, `delete_aftr_n_sec: null`, or `kick_matrix_room_members_not_in_mapped_authentik_group_anymor: false`
   load without a warning, and the destructive default applies. The same is true for keys inside
   `per_authentik_group_pk_matrix_room_settings`. CHANGELOG ("the model forbids unknown keys")
   overstates this.
2. **`bot_access_token` and `oauth2` both set** is accepted (the descriptions say "exactly one");
   `oauth2` silently wins. **Neither set** loads fine and only fails inside `build_app`.
3. **URLs without a scheme** (`server_url: matrix.company.org`, `mas_admin.url`, `authentik_server.url`)
   are accepted and fail as an httpx error on the first request.
4. **MXIDs are not validated.** `bot_user_id: bot`, a `bot_user_id` on another server (the
   description says it must be on `server_name`), and `admin_room.admin_user_ids: ["admin"]` are all
   accepted. A bare username in `matrix_user_ignore_list` protects nobody, silently. That is the list
   meant to keep server admins and bridges away from kicks and deactivation.
5. **Intervals.** `server_tick_rate_sec: 0` or negative makes `asyncio.wait_for(..., timeout=0)` time
   out immediately. The engine then reconciles in a tight loop: a full read of Synapse, as fast as it
   can, forever. Negative `deactivate_after_n_sec` or `delete_after_n_sec` are accepted too.
6. **Alias fields** (`create_matrix_rooms_in_a_matrix_space.alias`, `admin_room.alias`) accept a full
   alias such as `#space:company.org`. `build_canonical` then produces `##space:company.org:company.org`
   and the bot creates a new space or room on every start.
7. **No-op combinations are not flagged.** `delete_disabled_rooms: true` without
   `disable_rooms_when_mapped_authentik_group_disappears`, or `place_onboarding_rooms_in_space: true`
   with the space disabled, are silently ignored.
8. `log_level` is `Literal["INFO", "DEBUG"]`, so `WARNING`, the level you want in production, is
   rejected. Meanwhile the `--log-level` CLI flag and `ONBOT_LOG_LEVEL` in `onbot/logging.py:23`
   accept anything.

Fix sketch: `model_config = ConfigDict(extra="forbid")` on every sub-model (this is a breaking
change for configs carrying stale keys, so mention it in CHANGELOG). Add `HttpUrl` or a scheme
check on URLs, an MXID pattern validator (`^@[^:]+:.+$`) on every MXID list and on `bot_user_id`
(including the `server_name` match), `PositiveInt` on `server_tick_rate_sec`, `NonNegativeInt` on
the cooldowns, an alias-localpart validator that rejects `#` and `:`, a `model_validator` for the
exactly-one-credential rule, and widen `log_level` to the standard levels.

### MNT-04 Lifecycle and room-setting descriptions contradict the code

- **Severity:** medium
- **Category:** docs
- **Location:** onbot/config.py:435-459, onbot/config.py:871-881, onbot/reconciler/engine.py:606, onbot/lifecycle/accounts.py:84-89, issues.md (entry 2)
- **Status:** confirmed (`decide_account_action(..., disabled_ts=100, now=1050, delete_after_n_sec=1000)` returns `wait`, at `now=1100` it returns `erase`; engine probe with a per-group `keep_updating_...: false` still renamed the room)
- **Effort:** S

These descriptions end up in `docs/CONFIG_REFERENCE.md` and `config.example.yml`, which are what
operators read:

- `deactivate_after_n_sec`: "before their Matrix account is **deactivated**". What actually happens
  at that point is **logout** (Synapse) or **lock** (MAS), which is reversible. Deactivation happens
  at the second stage. The field name has the same problem; `docs/features.md` describes it
  correctly.
- `delete_after_n_sec`: "counted from the same point as `deactivate_after_n_sec` (so it should be the
  larger of the two)". The code counts it from the logout timestamp (`now - disabled_ts`). With the
  defaults, erasure happens after 1 day plus 365 days, and the "should be larger" advice is not
  needed.
- `keep_updating_matrix_attributes_from_authentik` lives in `MatrixDynamicRoomSettings`, so the
  reference presents it as overridable per group. `engine.py:606` reads it only from
  `matrix_room_default_settings`. This is already issues.md entry 2 (its line reference, 524, is
  stale) and is still open. It is the only field of that class that bypasses `resolve_room_settings`.
- `enabled`: "disabled or deleted", see MNT-02.

Fix sketch: correct the three descriptions and regenerate the docs. Read the flag through
`resolve_room_settings`, or move it out of `MatrixDynamicRoomSettings`. Consider renaming
`deactivate_after_n_sec` to `logout_after_n_sec` with a deprecated alias.

### MNT-05 Destructive paths with no test in any tier, MAS lifecycle effectors untested

- **Severity:** medium
- **Category:** test
- **Location:** onbot/lifecycle/accounts.py:143-180 (`MasLifecycleEffectors`), onbot/clients/mas_admin.py:57-70, onbot/reconciler/engine.py:218-232 and 275-290, tests/integration/test_lifecycle.py, tests/unit/test_lifecycle.py:385-400
- **Status:** confirmed (`pytest tests/unit tests/contract --cov` missing lines; grep of tests/ for `MasLifecycleEffectors`, `unlock`, `deactivate_user`, `delete_after`, `disable_rooms`)
- **Effort:** M

Coverage is 90%, but the gaps are in exactly the code that does irreversible things:

- `MasLifecycleEffectors` is the production effector on every MAS deployment (the target topology).
  It has **zero** unit tests. Only the Synapse fallback (`AdminApiLifecycleEffectors`) is tested.
  `ApiClientMasAdmin.lock_user`, `unlock_user` and `deactivate_user` have no contract tests
  (mas_admin.py 57-70 uncovered), so a wrong endpoint path would only show up in production.
- In integration, `test_lifecycle.py` and `test_q1_experiment.py` cover only the logout (lock) stage,
  with `delete_after_n_sec = None`. Nothing in any tier covers MAS **erase** (deactivate), MAS
  **re-enable** (unlock), `include_user_media_on_delete` against a real server, or the MNT-02
  scenarios.
- Room teardown (`disable_rooms_when_mapped_authentik_group_disappears`, `delete_disabled_rooms`
  with `purge=True`) is covered only by unit tests against fakes. No integration test exercises the
  real block, kick and delete calls.
- In unit tests, the engine's group-room creation branch (275-286), unblock (289-290), and space
  creation, including the `create_matrix_space_if_not_exists.enabled=false` error (218-232), are
  uncovered. Unit tests also never exercise `_gather_orphaned_mxids` exclusions for the bot and
  ignore-listed users (656-659).
- Contract tests do cover four Synapse admin methods that production never calls (MNT-10). That is
  effort spent on dead code while the live destructive client methods go untested.

Fix sketch: add `respx` contract tests for the three MAS admin calls and unit tests for
`MasLifecycleEffectors` (unknown user means no call, erase with and without media). Add one
integration test per destructive stage (erase, reenable, room disable and delete).

### MNT-06 Unit tests are not hermetic: a local `config.yml` or `ONBOT_*` env changes results

- **Severity:** medium
- **Category:** test
- **Location:** onbot/config.py:991-1010 and 1359-1364, tests/ (no `conftest.py` in tests/ or tests/unit/), run_onbot.sh
- **Status:** confirmed (ran the fast suite with `ONBOT_CONFIG_FILE_PATH` pointing at a 4-line fake YAML: 15 failed, 362 passed)
- **Effort:** S

`OnbotConfig(...)` and `OnbotConfig.model_validate(...)` in tests still consult the settings sources:
`ONBOT_*` environment variables and the YAML file at `ONBOT_CONFIG_FILE_PATH`, which defaults to
`./config.yml`. `run_onbot.sh` and docs/development.md tell developers to keep a real `config.yml` in
the repo root, which is exactly where pytest runs. A fake YAML with
`create_matrix_rooms_in_a_matrix_space: {enabled: false}` plus `global_room_admins: ["ops"]` made
15 tests fail (test_engine, test_rooms, test_config, test_global_admins). A local config with
different values can also make a test pass for the wrong reason. A developer's real credentials end
up in the config objects of unit tests too.

Fix sketch: add an autouse fixture in `tests/conftest.py` that sets `ONBOT_CONFIG_FILE_PATH` to a
nonexistent path and deletes every `ONBOT_*` variable. The tests in test_config.py that want sources
set them explicitly anyway. About a dozen test modules also each build their own minimal config dict; one
shared `make_config(**overrides)` fixture would remove that duplication.

### MNT-07 Two lockfiles that resolve different versions; pdm is canonical, `uv.lock` is stray

- **Severity:** medium
- **Category:** maintainability
- **Location:** pdm.lock, uv.lock, .github/workflows/ci.yml, Dockerfile:26-33, docs/development.md, docs/project/BATTLE_PLAN.md:10
- **Status:** confirmed (`pdm lock --check` and `uv lock --check` both pass; a version diff of the two files shows 20 packages differing)
- **Effort:** S

pdm is the canonical tool: CI, the Dockerfile (`pdm export`), the release workflow, every `run_*.sh`,
`build_dev_env.sh`, and all docs use it, and BATTLE_PLAN records "PDM ... chosen over uv".
`uv.lock` was committed in e71bfcf and is maintained by nobody. The two files disagree on 20
packages, including runtime ones (`pydantic-settings` 2.14.2 vs 2.14.1, `anyio`, `certifi`,
`typing-extensions`) and the gates (`ruff` 0.16.1 vs 0.15.18, `mypy` 2.3.0 vs 2.1.0).

Consequences: "ruff and mypy clean" under `uv run` (this review's baseline) is not the check CI runs;
the Docker image ships different library versions than a uv dev environment; and `uv run` and
`pdm run` both sync the same `.venv`, fighting each other.

Fix sketch: pick one. Either delete `uv.lock` and add it to `.gitignore`, or migrate CI, Docker and
docs to uv and delete `pdm.lock`. The build backend (`pdm-backend`) can stay either way.

### MNT-08 Pre-commit hooks cannot run: pinned ruff 0.8.4 rejects the project config

- **Severity:** medium
- **Category:** maintainability
- **Location:** .pre-commit-config.yaml:10-15, pyproject.toml:56
- **Status:** confirmed (`uvx ruff@0.8.4 check onbot`: "unknown variant `py314`")
- **Effort:** S

The ruff hook is pinned to `v0.8.4`, which predates Python 3.14 support. It fails to parse
`target-version = "py314"` and could not parse the PEP 758 `except A, B:` syntax used throughout
`onbot/` either. With the hooks installed (as docs/development.md says), every commit fails. The
natural workaround, `git commit --no-verify`, also skips the **gitleaks** hook, which is the only
secret scan. BATTLE_PLAN Phase 2 claims a gitleaks CI job too, but `ci.yml` has none. The hook
config also lacks `mypy` and `check-config-docs`, although `scripts/gen_config_docs.py` and its
pdm script comment call the drift check a "CI/pre-commit" guard.

Fix sketch: bump the ruff hook to the locked version (or use a `local` hook running `pdm run ruff`
so the versions cannot drift), add `check-config-docs` as a local hook, and add a gitleaks step to
CI. Coordinate with 02 (supply chain).

### MNT-09 The reconciler has no dry-run; `DryRunEffectors` is dead in production and its docstring says otherwise

- **Severity:** medium
- **Category:** maintainability
- **Location:** onbot/reconciler/effectors.py:1-11 and 60-119, onbot/reconciler/engine.py:81, onbot/app.py:201-204, docs/project/GOALS.md (Q6)
- **Status:** confirmed (grep: `DryRunEffectors` is only constructed as the engine's default argument and in tests; `app.py` always passes `CSApiEffectors`)
- **Effort:** M

The effectors module docstring says `DryRunEffectors` exists "so `reconcile-once` runs end-to-end
against real read APIs without mutating anything (the dry-run-by-default safety principle, Q6)". That
was true in Phase 3. Today `reconcile-once` is fully live, and docs/authentik-groups.md recommends
it as the way to check your work. Even with `DryRunEffectors` wired in, the reconciler's most
sweeping writes bypass the seam: `add_user_to_room`, `room_set_blocked` and `delete_room` go straight
to `self.admin`. `DryRunEffectors.get_room_state` always returns `None`, so a dry-run pass would not
even compute a realistic plan. An operator therefore has no way to preview the kicks, blocks and
room deletions from MNT-01 before they happen. The lifecycle has `dry_run`; the reconciler does not.
GOALS Q6 asks for "dry-run for destructive actions".

Fix sketch: either delete `DryRunEffectors`, make `effectors` a required argument, and fix the
docstring, or build a real `--dry-run` for `reconcile-once`. That means a recording wrapper that
delegates reads to the live client and logs writes, with the admin-API writes routed through the
same seam. The second option is worth it given MNT-01.

---

## Low

### MNT-10 Dead code and dead API surface in `onbot/`

- **Severity:** low
- **Category:** simplification
- **Location:** listed below
- **Status:** confirmed (`uvx vulture onbot --min-confidence 60`, each hit checked by grep over onbot/ and tests/; pydantic fields and `__aexit__` arguments were false positives and are omitted)
- **Effort:** S

Only tests reference these:

- `onbot/clients/synapse_admin.py:111` `make_room_admin`, `:115` `set_user_server_admin_state`,
  `:91` `get_room_details`, `:96` `list_user_media`. `set_user_server_admin_state` can make any
  user a **server admin**. Unused, powerful methods are an attack and mistake surface with nothing in
  return. They are kept alive by contract tests (MNT-05).
- `onbot/events.py:24` `Signal.drift_detected`: never emitted, never subscribed. ADR-0004 still
  names "drift detected" as one of the two sanctioned bus signals.
- `onbot/reconciler/power_levels.py:123` `legacy_user_matches_or_outranks_creator`: the "selection
  predicate for the destructive `recreate-dm-rooms` migration". That command was never built (todo.md
  Session A, item 4), so this is test-only. ADR-0011 and notice_board.py still refer to the migration
  as if it existed.
- `onbot/models.py:59` `MappedUser.matrix_obj`: written in engine.py:194, never read.
- `onbot/admin/commands.py:32` `Command.is_known`: test-only.
- `DryRunEffectors`, see MNT-09.

Fix sketch: delete them, together with their tests. If the migration is still planned, keep the
predicate but say so in todo.md.

### MNT-11 The documented `onbot reconcile-once --log-level DEBUG` fails with an argparse error

- **Severity:** low
- **Category:** docs
- **Location:** onbot/cli.py:29-31, docs/authentik-groups.md:324, run_onbot_reconcile_once.sh:8, run_onbot.sh:6
- **Status:** confirmed (`uv run onbot reconcile-once --log-level DEBUG` gives "error: unrecognized arguments: --log-level DEBUG")
- **Effort:** S

`--log-level` is defined on the root parser, so it must come **before** the subcommand. The
"Checking your work" step in authentik-groups.md, the first thing an operator runs when a group did
not become a room, and the usage examples in both run scripts put it after, and fail. The same
applies to `docker run dzdde/onbot reconcile-once --log-level DEBUG`, because the image's entrypoint
is `onbot` and the command is `run`.

Fix sketch: attach the flag to every subparser via `parents=[common]` (keep the root one too), and
add a CLI test for the documented form.

### MNT-12 Planning and docs drift: todo.md, issues.md, CHANGELOG, BATTLE_PLAN, architecture.md, tests/README, stale code comments

- **Severity:** low
- **Category:** docs
- **Location:** listed below
- **Status:** confirmed (each item checked against `git log`, `git tag`, the code, and a relative-link checker over all tracked `*.md` outside legacy/)
- **Effort:** S

- **todo.md:** the intro still says "Two self-contained work packages" (there are R, A, B, C, D). B and
  C are struck through. **R** (f395e00, `room_version`, ADR-0011) and **D** (642a9a2, ADR-0012,
  lobby code and tests) are implemented but not marked done. **A** is mostly done (c3f6fe8: notice
  board, force-join, power-level self-heal), but its `onbot recreate-dm-rooms` migration and the
  troubleshooting note on pre-existing rooms are missing, and nothing says so.
- **issues.md:** entry 1 (lobbies never re-sync name or topic) is fixed (engine.py:476 and
  `test_lobby_name_and_topic_follow_the_group_room`) but still listed as open. Entry 2 is open
  (MNT-04), but its line reference (524) now points elsewhere (the code is at 606).
- **CHANGELOG.md:** the header says "Versions are not yet published", but there are 13 tags from
  `0.0.1-beta.1` to `0.0.10` (2026-08-04). Everything sits under `[Unreleased]`, with no per-version
  sections. There are no entries for lobby rooms, room version 12, `global_room_admins`, Authentik
  admin groups, `authentik_poll_rate_sec` and the discovery poller, or the notice-board and
  force-join welcome room. The `storage_dir` removal note claims the model forbids unknown keys,
  which is only true at the top level (MNT-03).
- **docs/project/BATTLE_PLAN.md:** Phase 8 is all unchecked, although the Dockerfile, release
  workflow, README rewrite, config metadata, and psyplus docs all exist. The status of the Phase 1
  maintainer items (rotation, history scrub) is unknown and should be confirmed. The "gitleaks CI
  job" was never built (MNT-08). Links on lines 89-133 point to legacy paths relative to
  `docs/project/` and are broken.
- **docs/architecture.md:60:** links to `../GOALS.md` and `../BATTLE_PLAN.md` are broken (the files
  are in `docs/project/`). Its ADR list stops at 0010, missing 0011 and 0012. The ADR README index
  itself is complete.
- **tests/README.md:** says `pdm run pytest` runs "unit + contract". With `testpaths = ["tests"]` and
  no default marker filter it runs the integration suite too, which needs Docker. docs/testing.md
  correctly calls the same command the full suite.
- **Stale code comments:** pyproject.toml:17-19 ("Matrix client library decision ... deferred to a
  Phase 6 ADR", decided in ADR-0008), config.py:12 (same), effectors.py:7-10 (MNT-09),
  lifecycle/accounts.py:23-25 (Q1 still "open question", although BATTLE_PLAN records it as answered
  empirically in Phase 7b), identity.py:9 ("later, onboarding"). There are 81 references to
  `Phase n`, `AD-n` or `BATTLE_PLAN §` in `onbot/`. `AD-n` is a second numbering scheme next to the
  ADR files.

Fix sketch: one docs pass. Strike R and D, split A into done and remaining, close issues entry 1,
restructure CHANGELOG into per-tag sections (or at least drop the "not yet published" claim), tick
Phase 8, fix the links, and replace `AD-n` with `ADR-000n` in code comments.

### MNT-13 Authentik user to MXID resolution is implemented four times with drifting rules

- **Severity:** low
- **Category:** simplification
- **Location:** onbot/reconciler/engine.py:164-185 and 647-662, onbot/admin/admins.py:46-66, onbot/reconciler/global_admins.py:75-152, onbot/discovery.py:40-50
- **Status:** confirmed (code reading)
- **Effort:** S

"Skip if on `authentik_user_ignore_list`, compute the MXID with
`authentik_username_mapping_attribute`, handle `KeyError`" is written out separately in the engine's
active-user gather, the engine's orphan gather, the admin allowlist, and the global-admin resolver,
and partly again in the discovery fingerprint. The copies have already drifted:

- `user["username"]` (would raise on a user without the key) versus `user.get("username")`;
- the active-user path logs a warning on an unmappable user, the orphan path skips it silently;
- `matrix_user_ignore_list` is applied in the engine and the listener but not to global admins. A
  user on both lists gets PL 100 in every room although the ignore list promises the bot "will not
  change their power level".

A future change to the mapping contract (the most critical contract in the project, per
docs/configuration.md) has to be made in four places.

Fix sketch: one `resolve_mxid(user, config) -> str | None` in `identity.py`, carrying the ignore-list
and logging policy, used by all four callers.

### MNT-14 Module layering: shared state kernel lives in `reconciler/`, one-module `rooms/` package, healthcheck imports the composition root

- **Severity:** low
- **Category:** maintainability
- **Location:** onbot/reconciler/state.py, onbot/rooms/admin.py, onbot/healthcheck.py:23, onbot/admin/control_room.py, onbot/media.py:14 together with onbot/clients/matrix.py:32 and 528, onbot/reconciler/engine.py
- **Status:** confirmed (import graph from grep of `from onbot` across onbot/)
- **Effort:** M

There are no import-time cycles, and the clients never import domain code. Against ADR-0004
("could later be split into its own process"), though:

- `reconciler/state.py` (the onbot state-event schemas and `event_type_name`) is imported by
  onboarding, lifecycle, `rooms/admin.py` and `avatar.py`. It is a shared kernel living inside one
  domain. Splitting onboarding or lifecycle out would drag the reconciler along.
- `onbot/rooms/` is a package with one module, the admin control-room provisioner, while the rest of
  that feature lives in `onbot/admin/`. todo.md and the code docstrings have to explain the split.
- `healthcheck.py` imports `onbot.app` (the composition root) for `build_matrix_token_provider`,
  which belongs in `onbot/auth/`.
- `admin/control_room.py` imports `ReconcilerEngine` only to read `last_reconcile_at`.
- `media.py` and `clients/matrix.py` import each other. The cycle is broken with a function-local
  import in `CSApiEffectors`.
- `ReconcilerEngine` (663 lines) mixes reading, room creation, lobbies, avatars, membership, power
  levels, attribute repair, and the lifecycle's orphan detection (`_gather_orphaned_mxids`, which
  belongs in lifecycle).

Fix sketch: move `reconciler/state.py` to `onbot/state.py`, `rooms/admin.py` to
`admin/provisioner.py`, and `build_matrix_token_provider` to `auth/`. Give the control room a small
`ReconcileStatus` protocol. Move orphan gathering into `lifecycle/`.

### MNT-15 Tooling leftovers: committed Black editor config, four config-template entry points, coverage gate split no longer justified

- **Severity:** low
- **Category:** maintainability
- **Location:** .vscode/settings.json, gen_config_docs.sh, scripts/gen_config_docs.py:44-54, pyproject.toml:48-51 and 96-114, onbot/config.py:1376-1407, run_tests.sh:4-7, .github/workflows/ci.yml:55
- **Status:** confirmed (file reading; the fast suite reports 90.12% against `fail_under = 88`)
- **Effort:** S

- `.vscode/settings.json` (tracked) sets `ms-python.black-formatter` as the Python formatter. The
  project formats with ruff at line length 110. Format-on-save in VS Code rewraps to Black's 88
  columns, and CI's ruff format then auto-commits it back.
- Config templates can be produced four ways: `./gen_config_docs.sh`, `scripts/gen_config_docs.py`,
  `pdm run gen-config-docs`, and `onbot generate-config`. The last one is a separate, comment-free
  dump implemented in `config.py`. The generated banners tell readers to use the shell script, while
  development.md and configuration.md say `pdm run`. The shell wrapper only adds a psyplus presence
  check.
- `run_tests.sh` and the fast CI job disable the coverage gate (`--cov-fail-under=0`) because the
  fast suite "cannot reach app.py". It already reaches 90% against the 88% floor, so a unit-coverage
  regression is only caught by the slow integration job. The pyproject comment calls the floor
  "meant to ratchet upward", but it has not moved past the Phase 7 value.

Fix sketch: remove `.vscode/` from the repo (or point it at the ruff extension); keep one config-doc
entry point (the pdm scripts) and fix the banners; gate the fast job at its own floor (for example
88) and keep the full-suite gate higher.
