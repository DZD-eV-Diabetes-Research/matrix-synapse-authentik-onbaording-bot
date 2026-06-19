# `legacy/` — pre-revival code (reference only)

This is the **original, pre-Matrix-2.0 bot** as it stood before the revival described in
[`../BATTLE_PLAN.md`](../BATTLE_PLAN.md). It is kept **only as a porting reference** while the
valuable business logic is migrated into the new `onbot/` package (Phases 3–5).

**Do not import, run, or extend this code.** It is intentionally excluded from the build,
linting, type-checking and tests. Known bugs and security issues are catalogued in
`BATTLE_PLAN.md` §3. The credential that used to live in `onbot/test.py` has been removed;
that token (and the others in the maintainer's local `get_access_token.sh` / `config.dev.yml`)
must still be **rotated** and the git history **scrubbed** — see `BATTLE_PLAN.md` Phase 1.

Once a module's logic has been ported and tested, its legacy counterpart here can be deleted.
