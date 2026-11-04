# Code review 2026-11-04

Full review of `onbot/` plus CI, packaging, git history, tests and docs. Goal: find bugs, security
problems, things to simplify, and things to restructure for robustness. This directory holds findings
only; fixes happen in later sessions (see `SESSIONS.md` once it exists).

Baseline at review start (commit `2c2e3a1`): 377 unit + contract tests green, `ruff check` clean,
`mypy --strict` clean. `legacy/` is out of scope.

## Layout

| File | Area |
|---|---|
| `01-security-code.md` | Security of the application code (auth, tokens, admin room, HTML, media fetching, secrets in logs) |
| `02-supply-chain.md` | Dockerfile, CI and release workflows, scripts, git history secret scan |
| `03-reconciler.md` | Reconciler engine and passes (rooms, membership, power levels, join rules, discovery) |
| `04-lifecycle.md` | Offboarding lifecycle, MAS and Synapse admin effectors, ledger |
| `05-runtime-clients.md` | HTTP clients, retries, sync pump, app wiring, shutdown, CLI, healthcheck |
| `06-onboarding-admin.md` | Welcome flow, notice board, admin control room, broadcast, avatar, media |
| `07-maintainability.md` | Config model, dead code, structure, tests, docs drift, tooling |
| `08-live-verification.md` | Findings checked against the local integration stack |
| `DECISIONS.md` | Maintainer decisions taken during the review |
| `SUMMARY.md` | Consolidated, deduplicated, prioritised findings |
| `SESSIONS.md` | Follow-up work cut into session-sized chunks |

## Finding format

Every finding in the area files uses this shape:

```
### <ID> <short title>

- **Severity:** critical | high | medium | low | info
- **Category:** bug | security | robustness | simplification | maintainability | docs | test
- **Location:** path/to/file.py:123 (more if needed)
- **Status:** confirmed (how) | plausible | needs live stack
- **Effort:** S (< 1h) | M (half day) | L (a day or more)

What is wrong, the concrete failure scenario (inputs, state, outcome), the evidence, and a fix sketch.
```

ID prefixes: `SEC`, `SUP`, `REC`, `LIF`, `RUN`, `ONB`, `MNT`.
