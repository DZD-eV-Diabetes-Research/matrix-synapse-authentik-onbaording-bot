# Review summary 2026-11-04

Consolidated view over the area files `01` to `08`. Findings that several reviewers reported
independently are merged under one theme; the original IDs stay valid and point to the detailed
write-ups. Maintainer decisions are in `DECISIONS.md`, the fix plan in `SESSIONS.md`.

Numbers: 116 findings from seven reviewers plus 3 from live verification (LIV). After deduplication
about 95 distinct issues. 14 checks ran against the live integration stack (`08-live-verification.md`):
12 confirmed (some worse than described), 1 partially, 1 refuted.

**Legend:** `live` = reproduced on the local Synapse + MAS + Authentik stack, `exp` = reproduced by a
unit-level experiment, `code` = confirmed by reading the code.

## Read this first

The ten issues that matter most, in order:

| # | Issue | IDs | Evidence |
|---|---|---|---|
| 1 | Two real `syt_` tokens in public git history (already invalid per D1; scrub planned per D2) | SUP-01 | gitleaks |
| 2 | Offboarding fails open: a locked user who is deleted in Authentik (or leaves scope, or the user list collapses) is MAS-unlocked, old tokens work again, and nothing locks them again | LIF-01, SEC-01, MNT-02 | live |
| 3 | Users deleted in Authentik without being disabled first are never offboarded | LIF-02 | live |
| 4 | No guard against a collapsed Authentik answer: a token that can see one user made one pass kick every group member | REC-01, LIF-08 | live |
| 5 | `sync_authentik_users_with_matrix_rooms.enabled: false`, or a user filter matching nobody, kicks everyone from every group room | REC-02, MNT-01 | exp |
| 6 | One failing room aborts the whole pass on every tick, before offboarding runs | REC-03, SEC-04, LIF-04 | live |
| 7 | Rooms are paired with groups by canonical alias only: a moderator clearing the alias breaks every pass, a squatted alias aborts passes or force-joins members into an attacker's room | REC-04, SEC-02, REC-13 | live |
| 8 | In pre-v12 rooms the bot grants PL 100 it can never withdraw; the failed demotion then aborts every pass | REC-05 | live |
| 9 | Concurrent welcomes lose `m.direct` entries: 9 of 20 users got a second notice board and a second welcome after restart | RUN-01, ONB-01 | live |
| 10 | Any account that joins a bot room (MAS-only, federated) gets a notice board and every future broadcast, and keeps it after being kicked | SEC-06, ONB-13 | live |

Runner-up, all live: the bot never recovers from cold-start failures (RUN-02), the sync pump never
recovers from an invalid position so the admin room goes deaf (RUN-03, ONB-14), server-side default
encryption makes the admin room silently deaf (ONB-06), `docker stop` ends in SIGKILL (RUN-09), and a
MAS lock does not end sessions, so re-enable revives all of them (LIF-07).

## What is in good shape

- No access token appears in any log line, even at DEBUG (`01`, checked against a local server).
- HTML injection through welcome messages and placeholders is not possible: the markdown renderer
  escapes raw HTML and drops `javascript:` links.
- Every config field is read somewhere; generated config docs are in sync with the model.
- Nothing outside `legacy/` imports `legacy/`.
- With a working stack the full suite passes: 394 tests, 93.9 % coverage.

## Corrections between reviewers

- **Secrets in config validation errors:** `01` states pydantic truncates `input_value` so no secret is
  printed. That is wrong for short values. RUN-07 is correct; re-checked during consolidation:
  `onbot healthcheck` with a nested block missing `client_id` printed a short `client_secret` in
  clear text.
- **LIF-13 refuted live:** a MAS lock does not hide the user from the Synapse user list and does not
  set Synapse `locked`; there is no lock/unlock oscillation.
- **REC-01 refined live:** a token without any permission gets HTTP 403 (safe). The dangerous case is a
  token whose permissions shrink to a subset: 200 with a short list.
- **RUN-03 trigger refined live:** a Synapse restart does not invalidate the sliding sync position;
  connection expiry or purge does.
- **BATTLE_PLAN Phase 1 corrected by `02`:** `config.dev.yml` and `get_access_token.sh` were never
  committed. The Authentik key and the passwords are not in git history; only the two `syt_` tokens are.
- **issues.md:** entry 1 (lobby name/topic re-sync) is fixed in code but still listed; entry 2
  (per-group `keep_updating_...` override ignored) is still open (MNT-04).

## Themes

Each theme lists the merged IDs, the severity after verification, and the session in `SESSIONS.md`
that fixes it.

### T1 Offboarding correctness (critical)

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| LIF-01, SEC-01, MNT-02 | Fail-open re-enable: dropping out of the orphan set unlocks in MAS | critical | live | S1 |
| LIF-02 | Deleted Authentik users never offboarded | high | live | S1 |
| LIF-03 | One failing user stalls everyone; ledger saved only at pass end, erases repeated | high | exp | S1 |
| LIF-05, REC-14 | Active and disabled queries use different scope filters | medium | exp | S1 |
| LIF-06 | Dry-run performs live MAS unlocks, including locks onbot never set | medium | exp | S1 |
| LIF-10 | MAS lookup 404 is a silent no-op audited as success | medium | code | S1 |
| LIV-02 | Audit line "erase in Nones" | low | live | S1 |
| LIF-07, LIV-03 | MAS lock does not end sessions; token cache delays lock by ~2 min | medium | live | S4 |
| LIF-09 | Silent Synapse-admin fallback under MAS, audit claims sessions revoked | medium | code | S4 |
| LIF-11 | Erase semantics differ per effector (decision D7) | medium | live | S4 |
| LIF-12 | Media deletion stops after 100 files | medium | code | S4 |
| LIF-14 | Ledger: corrupt blob stalls lifecycle, lost ledger forgets locks, unbounded growth | low | code | S4 |
| LIF-15, MNT-04 (part) | Lifecycle docs contradict code | low | code | S4 |
| LIF-16, MNT-05 | MAS effectors, erase, re-enable untested in every tier | medium | code | S1, S4 |
| LIF-13 | Lock/unlock oscillation | refuted | live | none |

### T2 Blast radius of destructive actions (decision D4)

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| REC-01, LIF-08 | No plausibility check on Authentik input, no per-pass limit | high | live | S3 |
| REC-02, MNT-01 | `enabled: false` or an empty user filter kicks everyone | high | exp | S2 |
| REC-06 | Transient admin-group lookup failure kicks and demotes global admins | medium | exp | S2 |
| REC-07 | `matrix_user_ignore_list` users are kicked | medium | exp | S2 |
| MNT-09 | Reconciler has no dry-run; `DryRunEffectors` is dead code | medium | code | S3 |
| (D4 layer 3) | Irreversible actions above a threshold need `!confirm` | new | | S5 |

### T3 Pass robustness

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| REC-03, SEC-04, LIF-04 | One failing room aborts the pass; offboarding runs last | high | live | S2 |
| REC-05 | Irrevocable PL 100 in pre-v12 rooms | high | live | S2 |
| REC-12 | Bot's own MXID can be written into the PL users map | low | code | S2 |
| REC-08 | PL not withdrawn when a user leaves the group | medium | exp | S2 |
| MNT-04 (part), issues.md #2 | Per-group `keep_updating_...` override ignored | medium | code | S2 |
| RUN-08 | Reconcile pass blocks on onboarding via the awaited event bus | medium | exp | S8 |
| REC-16 | Thousands of serial API calls per pass at scale | low | code | S14 |
| REC-17 | Engine structure (three loops, duplicated group/lobby paths) | info | code | S14 (optional) |

### T4 Room identity (decision D3)

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| REC-04, SEC-02 | Rooms paired by canonical alias only; squatting and alias changes break passes | high | live | S6 |
| REC-13 | Two groups with the same alias fight over one room | low | code | S6 |
| ONB-17 | Control room alias trusted without checking the marker | low | code | S6 |
| ONB-16, ONB-02 | Room provisioning implemented three times; marker written after `createRoom` so a failure in between skips setup forever | medium | exp | S6 |
| REC-09 | Space link and state marker never repaired | medium | code | S7 |
| REC-10 | Lobby switched off stays open; manual close reverted | medium | exp | S7 |
| REC-11 | Bot unblocks rooms a human server admin blocked | low | code | S7 |

### T5 User identity (decision D8)

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| SEC-07 | MXID from a mutable attribute: renames break the mapping, collisions silent | low, raised by D8 | code | S9 |
| MNT-13 | MXID resolution written four times with drifted rules | low | code | S9 |
| SEC-08 | URL path segments not encoded (`x/../victim` reaches `victim`) | low | exp | S9 |

### T6 Onboarding and notice boards

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| RUN-01, ONB-01 | Concurrent welcomes lose `m.direct` entries | high | live | S8 |
| SEC-06, ONB-13 | Any joiner gets a notice board and broadcasts; startup replays every member | medium | live | S8 |
| ONB-06 | Default encryption makes control room (and notice boards) encrypted and deaf | medium | live | S8 |
| ONB-07 | User who left the notice board cannot return; broadcast counts it as reached | low | code | S8 |
| ONB-08 | Unreadable DM fails the welcome every tick | low | code | S8 |
| ONB-09 | Welcome is at-least-once | low | code | S8 |
| ONB-18 | Message identity is the exact text; a typo fix re-sends to everyone | info | code | S8 |
| ONB-15 | Avatars keyed by URL only | low | code | S12 |
| ONB-05 | Legacy cutover re-welcomes everyone (D5: docs note only) | medium | code | S14 |

### T7 Runtime and HTTP layer

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| RUN-02 | Cold-start failures never retried; runs degraded forever | high | live | S10 |
| RUN-03, ONB-14 | Sync pump never recovers from `M_UNKNOWN_POS` | medium | live | S10 |
| RUN-04, SUP-09, RUN-11 | No liveness signal; healthcheck probes upstreams with fresh MAS tokens every 60 s | medium | code | S10 |
| RUN-09, SUP-10 | Shutdown unbounded; SIGKILL on `docker stop` | medium | live | S10 |
| RUN-12 | `build_app` leaks clients on failure | low | code | S10 |
| RUN-13 | One-shot commands run all startup side effects | low | code | S10 |
| RUN-05 | Non-idempotent POSTs retried blindly | medium | exp | S11 |
| RUN-06, SEC-10 | Token provider: no single-flight refresh, no 401 invalidation | medium | exp | S11 |
| RUN-10 | `retry_after_ms` ignored, 60 s timeout everywhere | low | exp | S11 |
| RUN-14 | Offset pagination skips entries when the listing changes | low | code | S11 |
| RUN-15 | httpx logs every request at INFO | low | exp | S11 |
| RUN-16 | Dead version gating, duplicated request paths | low | code | S11 |

### T8 Broadcast and admin room

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| ONB-03 | Broadcast fails for most rooms if the rate-limit override did not apply | medium | exp | S12 |
| ONB-04 | `!announce` reply exceeds the event size limit after ~190 failures | medium | exp | S12 |
| ONB-10 | Announcements show raw markdown | low | code | S12 |
| ONB-11 | Edit without `* ` prefix re-runs `!announce` | low | exp | S12 |
| ONB-12 | Admin who leaves the control room is re-invited every tick | low | code | S12 |
| SEC-09 | Admin allowlist trusts Authentik's server-side group filter | low | code | S12 |
| SEC-12 | Replay buffer written before the allowlist check | info | code | S12 |

### T9 Application security hardening

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| SEC-03 | Group avatar URL is a full-read SSRF, no size cap, no total timeout | medium | code | S13 |
| RUN-07, SEC-11 | Config validation errors print secrets; secrets are plain `str` | medium | exp | S13 |
| MNT-03 | Config accepts wrong values (typos in nested blocks, both/neither credential, no URL scheme, bad MXIDs, tick <= 0, alias with `#`/`:`) | medium | exp | S13 |
| SEC-05 | `chatroom_params` unvalidated (D6: docs only) | medium | code | S13 |

### T10 Supply chain, CI and release

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| SUP-01 | Leaked tokens in history (D1 invalid, D2 scrub) | critical | gitleaks | S16 |
| MNT-08, SUP-03 | Pre-commit cannot run (ruff 0.8.4 vs py314); no secret scan anywhere | medium | exp | S0 |
| MNT-07, SUP-11 | `uv.lock` stray and drifted; CI does not enforce `pdm.lock` | medium | exp | S0 |
| MNT-06 | Unit tests not hermetic (local `config.yml`, `ONBOT_*`) | medium | exp | S0 |
| LIV-01 | Integration runner exits 0 when every test is skipped | medium | live | S0 |
| MNT-11 | `onbot reconcile-once --log-level DEBUG` fails | low | exp | S0 |
| SUP-02 | Release not gated on CI, review or protected refs | high | code | S15 |
| SUP-04 | Write-scoped workflow token, tag-pinned actions, long-lived PyPI token | medium | code | S15 |
| SUP-05 | CI checks out `head_ref`; format commits land without CI | medium | code | S15 |
| SUP-06 | Docker image version 0.0.0 for PEP 440 beta tags | medium | code | S15 |
| SUP-07, SUP-08 | Stale base image, no Dependabot; build not reproducible | medium | code | S15 |
| SUP-12 to SUP-17 | Stray PyPI release, unbounded deps, provision script exposes token, test stack on 0.0.0.0, `build_dev_env.sh`, untracked files in image | low | code | S15 |

### T11 Maintainability and docs

| IDs | Issue | Sev | Evidence | Session |
|---|---|---|---|---|
| MNT-12, SUP-18, SUP-19, ONB-19 | Planning docs drift (todo.md sessions R/A/D, issues.md, CHANGELOG says nothing published, BATTLE_PLAN Phase 1, broken links) | low | code | S14 |
| MNT-10 | Dead code (`set_user_server_admin_state`, `Signal.drift_detected`, unused migration predicate) | low | code | S14 |
| MNT-14 | Layering: shared state in `reconciler/`, one-module `rooms/`, healthcheck imports app | low | code | S14 |
| MNT-15 | Black editor config, four config-template entry points, coverage gate split | low | code | S14 |
| REC-15 | Discovery fingerprint misses reconciler inputs | low | code | S14 |
