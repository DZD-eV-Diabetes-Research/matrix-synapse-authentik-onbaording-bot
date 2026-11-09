# `legacy/`: pre-revival code (reference only)

This is the **original, pre-Matrix-2.0 bot** as it stood before the revival described in
[`../docs/project/BATTLE_PLAN.md`](../docs/project/BATTLE_PLAN.md). It is kept **only as a porting
reference**; its logic was ported into the `onbot/` package (BATTLE_PLAN Phases 3 to 5).

**Do not import, run, or extend this code.** It is intentionally excluded from the build,
linting, type-checking and tests. Known bugs and security issues are catalogued in
`BATTLE_PLAN.md` §3. The two tokens that used to be in the repository history were already invalid,
and the history was scrubbed on 2026-11-06 (BATTLE_PLAN Phase 1).

**Moving a server from this bot to the new one?** Read
[Upgrading from the legacy bot](../docs/deployment.md#upgrading-from-the-legacy-bot) first: the
cutover opens new welcome rooms and re-sends the welcome messages to every user.
