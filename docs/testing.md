# Testing

Onbot has two tiers of tests: a fast suite (unit and contract, no live services) and a full suite
that adds integration tests against a live Synapse, MAS, Postgres, and Authentik stack.

## Fast suite

Unit and contract tests, no live stack. Runs in seconds and is the quick local feedback loop.

```bash
pdm run pytest -m "not integration"
# or the wrapper, which also checks the fast suite's coverage floor (90 %):
./run_tests.sh
```

`./run_tests.sh --dev` stops at the first failure with full output (`-x -s --tb=long`). Any other
arguments pass through to pytest, for example:

```bash
./run_tests.sh tests/unit/test_engine.py -k mxid
```

## Full suite (with the live stack)

The full suite adds integration tests. The Synapse, MAS, Postgres, and Authentik stack is brought up
automatically by the testcontainers fixture in `tests/integration/conftest.py`, so **Docker is
required**. The first run pulls images and boots the stack, which takes a couple of minutes.

```bash
pdm run pytest
# or the wrapper, which enforces the coverage gate:
./run_integration_tests.sh
```

This is the authoritative coverage gate. It runs the whole suite, so the composition root
(`onbot/app.py`), only reachable end-to-end, is exercised and counted.

If the stack cannot start, the run fails with a non-zero exit code; the integration tests are never
silently skipped. On a machine without Docker, run the fast suite, or set `ONBOT_ITEST_SKIP=1` to
skip the integration tests of a plain `pdm run pytest` explicitly.

Useful knobs:

- `ONBOT_ITEST_WORKERS=<k>` splits the run over `k` parallel workers, each with a stack of its own
  (2 to 3 GB of RAM per worker). See
  [tests/README.md](../tests/README.md#faster-runs).
- `ONBOT_ITEST_KEEP=1` leaves the stack running after the run, for fast local iteration.
  `./run_integration_tests.sh --down` tears down every stack of the instance.
- `ONBOT_ITEST_INSTANCE=<n>` runs an isolated stack instance with its own compose project and host
  ports, so several checkouts (for example git worktrees) can run the suite at the same time. See
  [tests/README.md](../tests/README.md#several-stacks-in-parallel-one-per-worktree).
- `--dev` stops at the first failure with full output.
- Other arguments pass through to pytest, for example `./run_integration_tests.sh -k lifecycle`.

The tests never read your local `config.yml` or `ONBOT_*` variables: a suite-wide fixture hides them.

## The localpart-contract test

One integration test specifically guards the MXID localpart contract described in
[docs/configuration.md](configuration.md#the-mxid-localpart-contract): it proves that Onbot's
computed MXIDs match the accounts MAS provisions from the same Authentik claim. If you touch MXID
computation or the username mapping, keep this test green.
