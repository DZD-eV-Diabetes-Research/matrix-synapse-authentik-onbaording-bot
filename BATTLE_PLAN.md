# Revival & Modernization Battle Plan

**Project:** `matrix-synapse-authentik-onbaording-bot` (Onbot)
**Goal:** Rebuild this pre-Matrix-2.0 bot on a clean architecture: a tidy, maintainable, well-tested project
targeting the modern Matrix protocol (MAS / next-gen auth, authenticated media, sliding sync), with up-to-date
dependencies and a real test suite (unit + integration against a live Matrix stack).
**Approach:** **Clean slate.** We design the architecture we want and port only the *valuable logic* from the
old code; we do not preserve the old structure.
**Plan authored:** 2026-06-19 · **Revised:** 2026-06-19 (architecture decisions agreed)

---

## 0. TL;DR — Phase Overview

| Phase | Theme | Outcome |
|-------|-------|---------|
| **1** | 🔴 Security triage | Rotate leaked credentials, purge secrets from repo/history |
| **2** | Project skeleton & tooling | New package layout, `pyproject.toml`/`uv`, lint/type/test/CI |
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

### Phase 1 — 🔴 Security triage (blocks everything)
- [ ] Rotate every exposed credential (tokens in [config.yml](config.yml)/[onbot/test.py](onbot/test.py),
      passwords in [get_access_token.sh](get_access_token.sh)).
- [ ] `git rm --cached config.yml`; ship `config.example.yml` only; gitignore real configs.
- [ ] Delete scratch scripts with secrets.
- [ ] Scrub history with `git filter-repo`; coordinate force-push (shared repo).
- [ ] Add `pre-commit` + `detect-secrets`/`gitleaks`.

### Phase 2 — Project skeleton & tooling
- [ ] Create the package layout (§4). `pyproject.toml` (PEP 621), package name `onbot`, console entry point.
- [ ] **`uv`** for deps + committed lockfile (drop `setup.py`/`reqs.txt`).
- [ ] `ruff` (lint+format), `mypy`, `pytest` (+`pytest-asyncio`, `respx`, `pytest-cov`), `pre-commit`.
- [ ] GitHub Actions CI: lint → typecheck → unit. Python 3.11–3.13.
- [ ] `docs/adr/` with AD-1…AD-7 from §1. Remove hard-coded absolute paths; sane config-path default.

### Phase 3 — Reconciler core (AD-2, AD-7)
- [ ] Async `clients/base.py` (httpx + tenacity retries + **pagination** + typed errors).
- [ ] Authentik + Synapse-Admin clients on the base (no Matrix deps → lowest risk first).
- [ ] `reconciler/engine.py`: idempotent desired-vs-actual diff/apply; **scheduled + on-demand** (no
      `while True`); graceful shutdown via signals; `reconcile-once` CLI mode.
- [ ] Port rooms/membership/space/power-level logic; fix the §3 bugs in the rewrite.
- [ ] Versioned onbot room-state schemas (`reconciler/state.py`).
- [ ] Unit + contract tests alongside (Phase 7).

### Phase 4 — Onboarding bot (AD-3)
- [ ] Matrix CS client + sliding-sync stream in `clients/matrix.py`.
- [ ] `onboarding/listener.py` consuming sliding sync; `welcome.py` flow made idempotent via room-state.
- [ ] `onboarding/identity.py`: MXID computation matching MAS localpart template (AD-6).
- [ ] Wire reconciler "new user" signal → onboarding (`events.py`).

### Phase 5 — Lifecycle, quarantined (AD-5)
- [ ] `lifecycle/accounts.py`: deactivate/delete with cooldowns, **dry-run + audit-log default**.
- [ ] Verify MAS deactivation-propagation behavior (§7 Q1) and define the bot's exact responsibility.
- [ ] Heaviest test coverage of any module (destructive).

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
- [ ] Coverage gate (start ~60% unit, ratchet up; lifecycle held higher).

### Phase 8 — Packaging, docs & release
- [ ] Rewrite `Dockerfile`: multi-stage, pinned digest, non-root, `.dockerignore`, `uv` install, healthcheck.
- [ ] CI publish image (GHCR) on tag; semver; CHANGELOG.
- [ ] Rewrite `README.md`: MAS-era setup (Authentik-as-upstream topology, MXID/localpart contract), config
      reference (auto-gen from pydantic), Docker/compose deploy, troubleshooting.

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
   the Matrix account automatically, or must the lifecycle module enforce it? (Defines Phase 5 scope.)
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
2. **PR2 — Skeleton & tooling:** layout + `pyproject`/`uv` + ruff/mypy/pytest + CI + ADRs. No behavior.
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
