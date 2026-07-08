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

The [release workflow](../.github/workflows/release.yml) runs when a **GitHub Release** is published
and pushes both the DockerHub image (`dzdde/onbot`) and the PyPI package. The **git tag is the single
source of truth for the version** — pdm-backend derives it from the tag (SCM), so there is no version
to hand-edit in `pyproject.toml` or `onbot/__init__.py`.

- Tick **"pre-release"** on the GitHub Release → image tagged `beta` + version; PyPI upload is a
  PEP 440 pre-release (installable only with `pip install --pre`).
- Leave it unticked → image tagged `latest` (+ `major.minor` / `major`); PyPI stable upload.

Use a PEP 440 tag (e.g. `0.2.0b1` for a beta, `0.2.0` for a full release). See
[CHANGELOG.md](../CHANGELOG.md) and the workflow header for the full flow.
