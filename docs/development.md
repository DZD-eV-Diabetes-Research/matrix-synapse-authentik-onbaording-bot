# Development

This guide is for contributors working on Onbot itself. To just run the bot, see the
[README](../README.md) and [docs/deployment.md](deployment.md).

## Requirements

- **Python 3.14**
- **[PDM](https://pdm-project.org/)**

## Setup

```bash
pdm install                 # create the venv and install deps (incl. dev + docs)
pdm run pre-commit install  # enable lint + secret-scan hooks
```

## Run locally

```bash
pdm run onbot --help
pdm run onbot reconcile-once
```

The `run_*.sh` helper scripts at the repo root wrap the same PDM commands with sensible defaults.
`run_onbot.sh` starts the service and points `ONBOT_CONFIG_FILE_PATH` at `./config.yml`.

## Lint, format, and type-check

```bash
pdm run ruff check .           # lint
pdm run ruff format --check .  # formatting
pdm run mypy onbot             # type check
```

Testing has its own guide: [docs/testing.md](testing.md).

## Config docs are generated

[docs/CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) and [config.example.yml](../config.example.yml) are
generated from the config model with [psyplus](https://pypi.org/project/psyplus/). After editing
[`onbot/config.py`](../onbot/config.py), regenerate them:

```bash
pdm run gen-config-docs      # rewrite both artifacts
pdm run check-config-docs    # fail if they drift (runs in CI)
```

## Build the image locally

```bash
docker build -t onbot:dev .
docker run --rm onbot:dev --help
```

## Releasing

Versioned images are published by the [release workflow](../.github/workflows/release.yml) when a
`v*` tag is pushed. See [CHANGELOG.md](../CHANGELOG.md) and the workflow header for the tag and
version flow.
