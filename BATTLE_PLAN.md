# Revival & Modernization Battle Plan

**Project:** `matrix-synapse-authentik-onbaording-bot` (Onbot)
**Goal:** Rebuild this pre-Matrix-2.0 bot on a clean architecture: a tidy, maintainable, well-tested project
targeting the modern Matrix protocol (MAS / next-gen auth, authenticated media, sliding sync), with up-to-date
dependencies and a real test suite (unit + integration against a live Matrix stack).
**Approach:** **Clean slate.** We design the architecture we want and port only the *valuable logic* from the
old code; we do not preserve the old structure.
**Plan authored:** 2026-06-19 · **Revised:** 2026-06-19 (architecture decisions agreed; Phase 1–2 executed —
PDM + Python 3.14 chosen over uv/3.11–3.13)

---

## 0. TL;DR — Phase Overview

| Phase | Theme | Outcome |
|-------|-------|---------|
| **1** | 🔴 Security triage | Rotate leaked credentials, purge secrets from repo/history |
| **2** | Project skeleton & tooling | New package layout, `pyproject.toml`/PDM (Py 3.14), lint/type/test/CI |
| **3** | Reconciler core | Idempotent Authentik→Matrix reconcile (rooms, membership, power levels) |
| **4** | Onboarding bot | Event-driven welcome/onboarding via sliding sync |
| **5** | Lifecycle (quarantined) | Account deactivate/delete with dry-run + audit defaults |
| **6** | Matrix 2.0 / MAS integration | Upstream-IdP-aware auth, authenticated media, current APIs |
| **7** | Test suite | Unit + contract + integration (Synapse+MAS+Authentik containers) |
| **8** | Packaging, docs & release | Docker, docs, CI publish |

Phases 1–2 are prerequisites. Phase 6 (MAS/auth) underpins 3–5 and should be spiked early. Phase 7 grows
*alongside* 3–5, not after.

---

## 1. Architecture decisions (the agreed principles)

These supersede the old design. Captured here as lightweight ADRs; promote to `docs/adr/` during Phase 2.

### AD-1 — Clean slate, reuse logic not structure
We rebuild from a designed architecture and port valuable *business logic* (group→room mapping rules,
power-level computation, the custom-room-state persistence idea, the config model). The old plumbing
(sync/async bridge, god-object `Bot`, per-call client churn) is discarded.

### AD-2 — Reconciliation, not events, for Authentik→Matrix state (level-triggered)
The Authentik sync is a **convergence problem**, modeled as a Kubernetes-style controller: compute *desired*
state (from Authentik) vs *actual* state (Matrix/Synapse) and apply the diff. Reasons we do **not** rely on
events here: Authentik gives no reliable, ordered, complete event stream; we must converge on restart and heal
out-of-band drift; destructive actions must derive from *current* state, never a possibly-stale event.
**Principle: the reconcile is idempotent and convergent; any event is only a trigger that runs the same
reconcile sooner.** Implementation: run on a schedule **and** on demand (replacing the old `while True: sleep`).

### AD-3 — The onboarding bot **is** event-driven
Welcome/onboarding reacts to Matrix events via `/sync` (Simplified Sliding Sync, MSC4186): user appears/joins →
onboard. Triggered by the reconciler's "user provisioned/new" signal and/or membership events. This gives
instant onboarding (no tick latency) and is the natural Matrix model.

### AD-4 — Separate the concerns as a **modular monolith**
Three bounded domains — **reconciler**, **onboarding**, **lifecycle** — in one repo/process, sharing async
API clients, behind clean internal boundaries. We do **not** start with microservices (distributed-systems
tax with no payoff at this scale), but each domain is designed to be *separable into its own process later*.
Coupling is explicit: reconciler emits a "new user" signal that onboarding consumes.

### AD-5 — Quarantine the destructive lifecycle
Account deactivate/delete (with cooldowns) is the scariest code in the repo. It lives in its own isolated
module with **dry-run + audit-log defaults**, separate credentials boundary, and the highest test rigor.

### AD-6 — Auth topology: Authentik is an **upstream IdP to MAS**
Chain: *Matrix client → MAS (the Matrix-facing OAuth2/OIDC provider) → Authentik (upstream IdP)*.
Consequences that reshape scope:
- **MAS auto-provisions Matrix accounts on first login.** The bot **no longer pre-creates accounts** — a whole
  category of old code and risk is deleted. The sync layer reduces to **group→room projection + membership +
  power levels** (plus quarantined lifecycle).
- **MXID mapping is owned by MAS, not the bot.** The bot must compute a user's MXID using the *same localpart
  template MAS uses* (derived from the Authentik claim, e.g. `preferred_username`/`sub`). The old
  `authentik_username_mapping_attribute` must be defined to *agree with* MAS config, not guess. This is the
  critical integration contract — get it wrong and users won't match.
- **Bot credentials under MAS:** admin token for the Synapse Admin API + a bot user for the CS API, obtained
  via `mas-cli manage issue-compatibility-token` (near-term) or OAuth2 client-credentials + refresh (future).
- **Lifecycle re-evaluation:** when Authentik disables a user, the *upstream* blocks new logins, but existing
  Matrix sessions/accounts may persist. Whether MAS propagates deactivation/lock is an **open question to
  verify** (§7 Q1); the lifecycle module remains valuable as the enforcement backstop either way.

### AD-7 — Async everything, one HTTP base client
Fully `async`; drop `synchronize_async_helper`. One pooled `httpx.AsyncClient` base (auth injection, retries,
pagination, typed errors) shared by the Authentik, Synapse-Admin, and Matrix-CS clients.

---

## 2. What the project is today (legacy overview)

A **polling reconciliation bot** syncing Synapse with [Authentik](https://goauthentik.io/). It runs an
infinite tick loop (`Bot.server_tik`, [onbot/bot.py](onbot/bot.py)) that each tick: mirrors Authentik
groups→rooms, mirrors users→room membership, creates per-user DMs with welcome messages, maintains a parent
space, deactivates/deletes orphaned accounts, applies power levels, and sets avatars.

It is **stateless on disk** (except nio's e2e store): it persists its own bookkeeping as **custom room state
events** `org.<reversed-server-name>.onbot.<type>`. This idea is good and we keep it (AD-1).

| File | Role | Fate |
|------|------|------|
| [onbot/main.py](onbot/main.py) | entry point, wiring | rewrite |
| [onbot/bot.py](onbot/bot.py) | god-object reconciliation engine (~1000 lines) | **split** into reconciler/onboarding/lifecycle |
| [onbot/config.py](onbot/config.py) | pydantic-settings YAML config | **port & refine** |
| [onbot/api_client_matrix.py](onbot/api_client_matrix.py) | CS API (nio + raw requests) | rewrite on async base client |
| [onbot/api_client_synapse_admin.py](onbot/api_client_synapse_admin.py) | Admin API | rewrite on async base client |
| [onbot/api_client_authentik.py](onbot/api_client_authentik.py) | Authentik API | rewrite on async base client |
| [onbot/power_level_manager.py](onbot/power_level_manager.py) | per-room power levels | **port logic** |
| [onbot/utils.py](onbot/utils.py) | helpers + async/sync bridge | keep dict helpers, drop bridge |
| [onbot/test.py](onbot/test.py), [test_.py](test_.py) | scratch scripts w/ **real creds** | **delete** |

---

## 3. Findings — the audit (still relevant for security + what to port)

### 🔴 Critical (security)
1. **Live credentials committed.** [config.yml](config.yml) (tracked) has a real `bot_access_token` +
   Authentik `api_key`; [onbot/test.py](onbot/test.py) hard-codes real `syt_…` tokens. Rotate + scrub history.
2. **Plaintext passwords on disk.** [get_access_token.sh](get_access_token.sh) has real admin/bot passwords
   (gitignored but real — rotate).
3. No secret-scanning guardrails.

### 🟠 Logic/correctness bugs to avoid carrying over
- `access_token.lstrip("Bearer ")` ([api_client_matrix.py:92](onbot/api_client_matrix.py#L92)) strips
  *characters*, corrupting tokens.
- Membership check uses substring `in` instead of `==`
  ([bot.py:496](onbot/bot.py#L496)).
- `delete_room` builds a body it never sends ([synapse_admin.py:111](onbot/api_client_synapse_admin.py#L111));
  `set_room_admin` ignores args ([:75](onbot/api_client_synapse_admin.py#L75)).
- **No pagination anywhere** → silently truncates users/rooms/members/media past page 1.
- `get_room_state_event` fetches *all* room state and linear-scans it
  ([api_client_matrix.py:331](onbot/api_client_matrix.py#L331)).
- Dead/`NotImplementedError` code, stray `print()` debugging, sticky power levels (never withdrawn).

### ✅ Worth porting (the value)
- Group→room mapping & attribute rules (`_get_matrix_room_attrs_from_authentik_group`).
- Power-level computation ([power_level_manager.py](onbot/power_level_manager.py)).
- Custom-room-state persistence pattern (give it versioned, validated schemas).
- Pydantic config model & YAML generation.
- The catalogue of *which* Synapse Admin / Authentik / CS endpoints are needed.

---

## 4. Target architecture

```
onbot/
├── __main__.py / cli.py        # `python -m onbot`: run | reconcile-once | generate-config | healthcheck
├── app.py                      # composition root: build clients, wire domains, lifecycle, signals
├── config.py                   # pydantic-settings models
├── logging.py                  # structured logging
├── models.py                   # domain models + versioned onbot room-state schemas
├── events.py                   # internal signal bus ("user provisioned", "drift detected")
│
├── auth/
│   └── token_provider.py       # MAS-aware: static/compat token OR OAuth2 client-creds + refresh
├── clients/
│   ├── base.py                 # async httpx: auth, retries (tenacity), pagination, typed errors
│   ├── matrix.py               # CS API (+ sliding sync stream)
│   ├── synapse_admin.py        # Admin API
│   └── authentik.py            # Authentik API
│
├── reconciler/                 # AD-2: level-triggered, idempotent
│   ├── engine.py               # desired-vs-actual diff + apply; scheduled + on-demand
│   ├── rooms.py                # group → room projection
│   ├── membership.py           # group membership → room membership
│   ├── power_levels.py         # ported power-level logic
│   ├── space.py                # parent space
│   └── state.py                # onbot room-state read/write
│
├── onboarding/                 # AD-3: event-driven
│   ├── listener.py             # sliding-sync consumer
│   ├── welcome.py              # DM + welcome-message flow (idempotent via room-state)
│   └── identity.py             # MXID mapping consistent with MAS localpart template (AD-6)
│
└── lifecycle/                  # AD-5: quarantined, dry-run + audit default
    └── accounts.py             # deactivate/delete w/ cooldowns, audit log
tests/{unit,contract,integration}/
```

Boundaries: `reconciler` emits a "user provisioned/new" signal on `events.py`; `onboarding` consumes it (or
membership events) and runs the welcome flow. `lifecycle` is invoked only by the reconciler's
desired-vs-actual result, behind dry-run. All three share `clients/` + `auth/`.

---

## 5. Phase-by-phase plan

### Phase 1 — 🔴 Security triage (blocks everything)  ⚠️ partially done 2026-06-19
- [ ] **⚠️ MAINTAINER:** Rotate every exposed credential — @dzd-bot Synapse tokens, the @admin/@dzd-bot
      login passwords (`get_access_token.sh`), and the Authentik `api_key` (`config.dev.yml`). *(only you can.)*
- [x] `git rm --cached config.yml`; ship `config.example.yml` only; gitignore real configs (`config*.yml`).
- [x] Delete scratch scripts with secrets (`git rm onbot/test.py`; legacy run scripts removed).
- [ ] **⚠️ MAINTAINER:** Scrub history with `git filter-repo`; coordinate force-push (shared repo — open Q6).
- [x] Add `pre-commit` + `gitleaks` (pre-commit hook **and** CI job).

### Phase 2 — Project skeleton & tooling  ✅ done 2026-06-19
- [x] Create the package layout (§4). `pyproject.toml` (PEP 621), package name `onbot`, console entry point.
- [x] **`PDM`** (`pdm-backend`) for deps + committed `pdm.lock` (dropped `setup.py`/`reqs.txt`/run scripts).
- [x] `ruff` (lint+format), `mypy`, `pytest` (+`pytest-asyncio`, `respx`, `pytest-cov`), `pre-commit`.
- [x] GitHub Actions CI: lint → typecheck → unit + secret scan. **Python 3.14** (`requires-python >=3.14`).
- [x] `docs/adr/` with AD-1…AD-7 from §1. Removed hard-coded absolute paths (deleted legacy run scripts).

### Phase 3 — Reconciler core (AD-2, AD-7)  ✅ done 2026-06-19
- [x] Async `clients/base.py` (httpx + tenacity retries + **pagination** + typed `ApiError`).
- [x] Authentik + Synapse-Admin clients on the base (paginated; §3 bugs fixed — token no longer
      `lstrip`-corrupted, `delete_room` sends its body, `make_room_admin`/`set_user_server_admin_state`
      hit real endpoints, `room_is_blocked` returns a bool).
- [x] `reconciler/engine.py`: idempotent desired-vs-actual convergence; **scheduled + on-demand**
      (`trigger()`), graceful shutdown via SIGINT/SIGTERM; `reconcile-once` + `run` CLI modes. Also
      ported config (`config.py`), dict helpers (`utils.py`), MXID mapping (`identity.py`, AD-6),
      domain models (`models.py`), and the `events.py` signal bus.
- [x] Ported rooms/membership/space/power-level logic as **pure** functions; fixed the §3 substring
      membership bug and the room-create-params split-path bug; **added power-level withdrawal (G8.4)**.
- [x] Versioned onbot room-state schemas (`reconciler/state.py`, `schema_version`).
- [x] Unit + contract tests (48 tests, 83% cov; pure logic 100%). CI green (ruff/format/mypy/pytest).
- ✅ **(Resolved in Phase 4):** the Matrix CS writes behind the `MatrixEffectors` seam now have a
      concrete impl (`clients/matrix.py::CSApiEffectors`); `app.py` wires it in place of
      `DryRunEffectors` (which stays for tests/`reconcile` dry-runs).

### Phase 4 — Onboarding bot (AD-3)  ✅ done 2026-06-19
- [x] Matrix CS client (`clients/matrix.py`, `ApiClientMatrix` on the async base client — AD-7,
      *not* a new library; that's the Phase 6 ADR) + **Simplified Sliding Sync** stream (MSC4186),
      normalised to `SyncResult` so the listener is transport-agnostic. Also resolves the Phase 3
      deferral: concrete `CSApiEffectors` (room/space create, kick, power levels, name/topic, custom
      state events).
- [x] `onboarding/listener.py` consuming the sync stream (welcomes on join events) **and**
      subscribing to the reconciler signal; `welcome.py` flow idempotent two ways — one DM per user
      (via `m.direct` account data) and each message once (content-hashed in the `direct_room`
      onbot state event, G4.3).
- [x] MXID computation matching the MAS localpart template (AD-6) — provided by the shared
      `onbot/identity.py` (built in Phase 3), reused by onboarding (no separate `onboarding/identity.py`).
- [x] Wired reconciler "user provisioned" signal → onboarding via `events.py` (`Signal.user_synced`);
      `app.py` runs the reconcile loop + sliding-sync listener concurrently.
- [x] Tests: Matrix client contract tests (`respx`), welcome idempotency + listener unit tests
      (61 tests total, gate green: ruff/format/mypy/pytest under Python 3.14).
- ⏭️ **Deferred to Phase 6:** the sliding-sync endpoint is still unstable — CS-API version
      negotiation and the MAS-auth/library ADR (kept behind `ApiClientMatrix.sliding_sync`).
      Placing onboarding DMs inside the managed space (G4.5) and the admin control room (G4.6) are
      not yet implemented.

### Phase 5 — Lifecycle, quarantined (AD-5)  ✅ done 2026-06-19
- [x] `lifecycle/accounts.py`: a **pure state machine** (`decide_account_action`) — detect →
      `mark` (start cooldown, no destructive action) → `logout` after `deactivate_after_n_sec`
      (revoke sessions, G9.2) → `erase` after `delete_after_n_sec` (deactivate + optional media,
      G9.4/G9.5) → `reenable` if the user returns first (G9.6). Effects isolated behind a
      `LifecycleEffectors` seam (`AdminApiLifecycleEffectors`) + a `LifecycleLedgerStore`
      (per-user bookkeeping as one versioned blob in the bot's Matrix **account data** — no DB,
      decoupled from the onboarding DM).
- [x] **Dry-run + audit-log default (Q6, AD-5):** new `dry_run` config flag (defaults `true`);
      while dry-run only non-destructive timestamps are recorded and every would-be destructive
      action goes to the dedicated `onbot.lifecycle.audit` log. Operators opt in explicitly.
- [x] Reconciler integration: `_gather_orphaned_mxids` scopes candidates to Matrix accounts whose
      Authentik user is **disabled** (never sweeping unrelated admin/service accounts); bot user +
      ignore lists excluded (G12.1). `app.py` wires `AccountLifecycleManager` into the engine.
- [x] Heaviest coverage: `lifecycle/accounts.py` at **100%** (exhaustive state-machine table +
      dry-run/live + multi-tick progression + ledger round-trip + effectors); engine orphan-scoping
      tests. Full gate green (ruff/format/mypy/pytest, 88 tests). Also fixed pre-existing mypy slips
      in `test_power_levels.py`/`test_skeleton.py` that were failing the gate.
- 🔬 **§7 Q1 — decision (2026-06-19): answer empirically, not by guesswork.** Whether MAS itself
      revokes sessions / locks the account on upstream Authentik disable will be settled by a
      dedicated experiment in the **Phase 7 integration harness** (real Synapse + MAS + Authentik),
      then this module's exact responsibility finalized from the observed facts. The module is the
      safe enforcement backstop regardless (it only ever *removes* access, behind dry-run); see
      ADR-0005 and the Phase 7 task below.
- ⏭️ **Deferred:** the rich, self-documenting `config.example.yml` regeneration (the new field
      `dry_run` is live in `config.py` but the example file's doc-generator is a Phase 8 deliverable).

### Phase 6 — Matrix 2.0 / MAS integration (spike early, lands across 3–5)
- [ ] `auth/token_provider.py`: static/compat token (`mas-cli issue-compatibility-token`) **and** OAuth2
      client-credentials + refresh; works under MAS *or* legacy auth.
- [ ] **Authenticated media (MSC3916):** uploads/downloads via `/_matrix/client/v1/media/*` with auth headers
      (bot/room/space avatars).
- [ ] **Library decision (ADR):** keep `matrix-nio` vs. drive CS API via base httpx vs. `mautrix-python` —
      judged on MAS/OAuth support, sliding-sync, maintenance, async fit, e2ee needs.
- [ ] **E2EE stance (ADR):** if the bot only sends plaintext + manages state, likely drop deprecated
      libolm; if it must operate inside e2ee rooms, plan the rust-sdk crypto path.
- [ ] CS API version negotiation (`/_matrix/client/versions`); centralize API versions.

### Phase 7 — Test suite (grows during 3–5)
- [ ] **Unit:** mapping rules, power-level calc, identity/MXID, config, room-state (de)serialization.
- [ ] **Contract:** each client via `respx` against recorded fixtures.
- [ ] **Integration:** `testcontainers`/compose with **Synapse + MAS + Postgres** + Authentik (real or
      mocked, with Authentik as MAS upstream). Assert end-to-end: group→room, membership, onboarding,
      deactivation cooldowns, power levels, media. Marked `@pytest.mark.integration`, own CI job.
- [ ] **🔬 Resolve §7 Q1 by experiment (per 2026-06-19 decision):** in the live harness, disable a
      user in Authentik and observe MAS/Synapse — do existing sessions get revoked / the account
      locked automatically, or do they persist? Record the facts, then finalize the lifecycle
      module's responsibility (redundant backstop vs. the enforcement path) and update ADR-0005 + §7.
- [ ] Coverage gate (start ~60% unit, ratchet up; lifecycle held higher).

### Phase 8 — Packaging, docs & release
- [ ] Rewrite `Dockerfile`: multi-stage, pinned digest, non-root, `.dockerignore`, `pdm install`, healthcheck.
- [ ] CI publish image (GHCR) on tag; semver; CHANGELOG.
- [ ] Rewrite `README.md`: MAS-era setup (Authentik-as-upstream topology, MXID/localpart contract), config
      reference (auto-gen from pydantic), Docker/compose deploy, troubleshooting.
- [ ] Overhaul all config meta data in `onbot/config.py`: Add meaningfull desc. Add meaningfull examples. Check typing hints.
- [ ] Generate config markdown docs with https://pypi.org/project/psyplus/
- [ ] Generate yaml template config with https://pypi.org/project/psyplus/ and provide examples how to use ut
---

## 6. Reuse vs. rebuild — quick reference

| Item | Decision |
|------|----------|
| Group→room mapping & attribute rules | **Port** logic, retest |
| Power-level computation | **Port** logic, add withdrawal |
| Custom-room-state persistence | **Keep** pattern, versioned schemas |
| Pydantic config model | **Port & refine** |
| Endpoint catalogue (which APIs) | **Reuse** as knowledge |
| Account pre-provisioning | **Delete** — MAS handles it (AD-6) |
| `synchronize_async_helper` / per-call nio churn | **Delete** — go async (AD-7) |
| God-object `Bot`, tick loop | **Delete** — reconciler + listener |
| Scratch tests with creds | **Delete** |

---

## 7. Open questions for the maintainer

1. **MAS deactivation propagation:** when Authentik disables a user upstream, does MAS revoke sessions / lock
   the Matrix account automatically, or must the lifecycle module enforce it?
   *Decision (2026-06-19): defer the answer to facts — settle it with an experiment in the Phase 7
   integration harness (real Synapse+MAS+Authentik), then finalize the lifecycle module's
   responsibility. The Phase 5 module ships now as the safe backstop either way.*
2. **MXID localpart template:** what localpart rule does (will) MAS use for accounts provisioned from
   Authentik? The bot's identity mapping must match it exactly (AD-6).
3. **E2EE requirement:** must the bot operate *inside encrypted rooms*, or is plaintext welcome + state mgmt
   enough? (Decides whether we drop libolm.)
4. **Library preference:** open to replacing `matrix-nio`, or prefer to keep it?
5. **Scale:** rough user/group/room counts? (Tunes caching/diffing vs. simple full reconcile.)
6. **History rewrite OK?** Force-push of scrubbed history affects collaborators/forks — acceptable?
7. **Authentik version** in use? (API v3 shape has evolved.)

*Resolved:* Authentik is an **upstream IdP to MAS** → bot does not pre-create accounts; sync scope is
group→room projection + membership + power levels + (quarantined) lifecycle.

---

## 8. Suggested execution order (first PRs)

1. **PR1 — Security:** rotate, remove tracked secrets, secret-scan pre-commit. (History scrub coordinated.)
2. **PR2 — Skeleton & tooling:** layout + `pyproject`/PDM (Py 3.14) + ruff/mypy/pytest + CI + ADRs. No behavior.
3. **PR3 — Base client + Authentik/Admin clients (async, paginated) + first unit/contract tests.**
4. **PR4 — Auth/TokenProvider spike + MAS topology validation (ADR).**
5. **PR5 — Reconciler engine + ported rooms/membership/power-level logic.**
6. **PR6 — Onboarding listener (sliding sync) + welcome flow + identity mapping.**
7. **PR7 — Lifecycle module (dry-run/audit) + authenticated media.**
8. **PR8 — Integration harness (Synapse+MAS+Authentik) + Docker + docs + release.**

---

### Primary protocol references
- [Matrix API playground / spec](https://playground.matrix.org/#overview) — Client-Server API (endpoints,
  versions, sliding sync) for Phases 3–6.
- [Synapse Source Code](https://github.com/element-hq/synapse) - Clone it for easy inspection
- [Synapse documentation](https://element-hq.github.io/synapse/latest/welcome_and_overview.html) — Admin API;
  re-validate every endpoint (paths/params have drifted). MAS/Synapse docs also mirrored on `element-hq.github.io`.
- [The latest api spec, machin readable](https://spec.matrix.org/latest/client-server-api/api.json)

### Matrix 2.0 / MAS context
- [MAS — Authorization & sessions](https://element-hq.github.io/matrix-authentication-service/topics/authorization.html)
- [MAS — Get an access token](https://element-hq.github.io/matrix-authentication-service/topics/access-token.html)
- [MAS — upstream OAuth2/OIDC providers](https://element-hq.github.io/matrix-authentication-service/setup/sso.html)
- [Better auth, sessions & permissions in Matrix](https://matrix.org/blog/2023/09/better-auth/)
- [matrix.org is now running MAS](https://matrix.org/blog/2025/04/morg-now-running-mas/)
</content>
