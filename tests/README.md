# Tests

Three tiers (BATTLE_PLAN.md Phase 7), grown alongside Phases 3 to 5:

- **`unit/`**: pure logic (mapping rules, power-level calc, identity/MXID, config,
  room-state (de)serialization). No network.
- **`contract/`**: each async client exercised against recorded fixtures with `respx`.
- **`integration/`**: end-to-end against a live **Synapse + MAS + Postgres + Authentik**
  stack. Marked `@pytest.mark.integration` and run in their own CI job.

Run the fast tiers:

```bash
pdm run pytest -m "not integration" # unit + contract, no Docker needed
pdm run pytest tests/unit tests/contract
pdm run pytest -m integration       # the heavy stack tests (Docker required)
./run_integration_tests.sh          # full suite + coverage gate (Docker required)
```

Plain `pdm run pytest` runs all three tiers, so it needs Docker as well.

## Hermetic by default

`tests/conftest.py` removes every `ONBOT_*` environment variable for each test and points
`ONBOT_CONFIG_FILE_PATH` at a file that does not exist. A `config.yml` in the repo root (where
`run_onbot.sh` expects it) or exported settings never reach a test. A test that wants a config file
or variables sets them itself with `monkeypatch`. The `ONBOT_ITEST_*` variables below are harness
settings, not onbot settings, and are left alone.

## The integration stack

`tests/integration/conftest.py` starts the stack from `tests/integration/stack/docker-compose.yml`
once per test session through testcontainers, waits until the MAS to Authentik login chain works,
and tears it down at the end.

A stack that cannot start **fails** the run. It is never reported as skipped, so a green run means
the live suite really ran. If the first `docker compose up` fails (typically stale containers of an
earlier run of the same project), the harness runs `down` once and retries before it gives up.

| Variable | Effect |
|---|---|
| `ONBOT_ITEST_KEEP=1` | Leave the stack running after the run. The next run reuses it, which is much faster. |
| `ONBOT_ITEST_INSTANCE=<n>` | Use isolated stack instance `n` (see below). Default `0`. |
| `ONBOT_ITEST_SKIP=1` | Skip the integration tests explicitly, for machines without Docker. `run_integration_tests.sh` refuses it. |
| `ONBOT_ITEST_PROJECT`, `ONBOT_ITEST_SYNAPSE_PORT`, `ONBOT_ITEST_MAS_PORT`, `ONBOT_ITEST_AUTHENTIK_PORT` | Override single values that `ONBOT_ITEST_INSTANCE` derives. Rarely needed. |

### Several stacks in parallel (one per worktree)

Every instance is its own compose project with its own published host ports, so several can run on
one machine at the same time. One variable selects everything:

| `ONBOT_ITEST_INSTANCE` | Compose project | Synapse | MAS | Authentik |
|---|---|---|---|---|
| `0` (default) | `onbot-itest` | 8008 | 8080 | 9000 |
| `1` | `onbot-itest-1` | 8018 | 8090 | 9010 |
| `2` | `onbot-itest-2` | 8028 | 8100 | 9020 |
| `n` | `onbot-itest-n` | 8008 + 10n | 8080 + 10n | 9000 + 10n |

Every published port binds to `127.0.0.1` only, so the stack (with its fixed, public admin
credentials) is not reachable from the network while it runs. The harness talks to it on
`localhost`.

Give each worktree its own number and set it for every test command in that worktree:

```bash
export ONBOT_ITEST_INSTANCE=2
./run_integration_tests.sh                 # or: pdm run pytest -m integration -k lifecycle
```

Tear an instance down (for example after `ONBOT_ITEST_KEEP=1`, or a crashed run) by project name:

```bash
docker compose -f tests/integration/stack/docker-compose.yml -p onbot-itest-2 down -v
```

How it fits together: `tests/integration/stack_instance.py` derives the project name and ports and
writes them into an env file that the harness passes to every `docker compose` call; the compose
file interpolates the same `ONBOT_ITEST_*` names, with instance 0 values as defaults.
`tests/integration/stack_api.py` builds its host URLs from the same object. Only host ports change
per instance. Container ports and the in-network URLs (`mas:8080`, `authentik-server:9000`,
`synapse:8008`) are identical in every instance, so the MAS issuer, the Authentik upstream issuer and
the redirect URIs in the stack configs stay valid; the login helper rewrites the in-network hostnames
to `localhost:<host port>` on each browser hop. Synapse's `public_baseurl` still names the instance 0
port: Synapse only advertises it to clients, and the harness never follows it.

Each instance boots six containers (Authentik server and worker, Postgres, Redis, MAS, Synapse), so
plan for roughly 2 to 3 GB of RAM per running instance.

Two runs in the **same** checkout also share `.coverage` and `.pytest_cache`. Separate worktrees
avoid that; in one checkout, give the second run its own `COVERAGE_FILE=.coverage.<n>`.

### Test fixtures contain secrets on purpose

`stack/mas/config.yaml`, `stack/synapse/homeserver.yaml` and `stack_api.py` hold fixed, public
test-only keys and client secrets so the stack boots without setup. They are allowlisted by path in
`.gitleaks.toml`. Never reuse them anywhere else, and never put a real credential into these files.
The sdist leaves out `tests/` entirely (`[tool.pdm.build] excludes` in `pyproject.toml`), so these
files are not published to PyPI.
