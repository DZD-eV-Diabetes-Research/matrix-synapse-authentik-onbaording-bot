# Tests

Three tiers (BATTLE_PLAN.md Phase 7), grown alongside Phases 3–5:

- **`unit/`** — pure logic: mapping rules, power-level calc, identity/MXID, config,
  room-state (de)serialization. No network.
- **`contract/`** — each async client exercised against recorded fixtures with `respx`.
- **`integration/`** — end-to-end against a live **Synapse + MAS + Postgres + Authentik**
  stack. Marked `@pytest.mark.integration` and run in their own CI job.

Run the fast tiers:

```bash
pdm run pytest                      # unit + contract
pdm run pytest -m "not integration" # explicit
pdm run pytest -m integration       # the heavy stack tests
```
