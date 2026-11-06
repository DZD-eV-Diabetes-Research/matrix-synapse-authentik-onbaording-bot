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

PDM is the only supported project tool, and `pdm.lock` is the only lockfile (CI, the Dockerfile and
the release workflow all read it). After changing dependencies in `pyproject.toml`, run `pdm lock`
and commit the result: CI runs `pdm lock --check` and fails on a stale lockfile. Do not commit a
`uv.lock` or any other lockfile; `uv.lock` is gitignored.

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
git archive --format=tar HEAD | docker build -t onbot:dev -
docker run --rm onbot:dev --version
```

Building from `git archive` sends only committed, tracked files into the build. A plain
`docker build -t onbot:dev .` works too, but then an untracked `*.py` file under `onbot/` would end up
in the image: `.dockerignore` is an allowlist (Python files under `onbot/`, `pyproject.toml`,
`pdm.lock`, `README.md`, `LICENSE` and two helper scripts), so it keeps out everything else, but it
cannot know what git tracks. Release images are built from a git context of the tagged commit.

The image is built from `pdm.lock` without installing PDM: `scripts/lock_requirements.py` turns the
lock into hash-pinned requirements (standard library only), pip installs them with
`--require-hashes`, wheels only, and the onbot wheel is built by the locked `pdm-backend` from the
`build` dependency group. Without `--build-arg PDM_BUILD_SCM_VERSION=<version>` a local image
reports version `0.0.0`.

## Dependency updates

[Dependabot](../.github/dependabot.yml) checks weekly and opens PRs for:

- **GitHub Actions.** Every action is pinned to a full commit SHA with the release in a comment;
  Dependabot updates both together.
- **The Docker base image** (`python:3.14.x-slim-trixie`, Debian 13, tag and digest, in both
  Dockerfile stages). Python patch releases only; moving to a new Python minor or Debian release is
  a deliberate change.
- **Python dependencies, partially.** Dependabot does not support PDM and does not read `pdm.lock`.
  It only sees the ranges in `pyproject.toml` and opens a PR when a release falls outside them (with
  the caps in place, that means a new major version). Such a PR changes `pyproject.toml` only, so
  CI's `pdm lock --check` fails until you run `pdm lock` on the branch and push. Updates inside the
  ranges are not proposed. Run them yourself, at least before each release:

  ```bash
  pdm outdated          # what is behind
  pdm update            # update pdm.lock within the ranges in pyproject.toml
  ```

  This gap is deliberate (review decision D12): onbot has no inbound port, talks only to trusted
  upstreams and has six hash-pinned runtime packages, so there is no pip-audit job and no Renovate.

Not covered by any updater: the integration stack images (pinned by digest only), the tini checksum
in the Dockerfile, the gitleaks version in `ci.yml` and `.pre-commit-config.yaml`, and the PDM version
(`PDM_VERSION` in both workflows, `ONBOT_DEV_PDM_VERSION` in `build_dev_env.sh`).

Version policy for runtime dependencies (`pyproject.toml`): each one is capped below its next major
release, so `pip install onbot` never picks up an untested major. The Docker image is the supported
install and always runs exactly the locked set. Raising a cap is a deliberate change: edit the range,
`pdm lock`, run the full suite.

## Releasing

The [release workflow](../.github/workflows/release.yml) runs when a **GitHub Release** is published
and pushes both the Docker Hub image (`dzdde/onbot`) and the PyPI package (`onbot`). The **git tag is
the single source of truth for the version**: there is no version to hand-edit in `pyproject.toml`
or `onbot/__init__.py`.

Steps:

1. Make sure CI on `main` is green and `pdm.lock` is current (see "Dependency updates"). CI skips the
   integration suite (about 20 minutes) for pushes and PRs that change documentation only; any other
   path runs it, and so does `CHANGELOG.md` (`scripts/ci_gate.py`, review decision D17).
2. Move the "Unreleased" entries in [CHANGELOG.md](../CHANGELOG.md) under `## [X.Y.Z] - DATE` and
   merge that to `main`.
3. Create a GitHub Release with a new tag on `main`. Tick **"Set as a pre-release"** exactly when the
   tag is a pre-release version.

Tags are PEP 440 versions with a three-part release number:

| Tag | Published version | Docker tags | PyPI |
|---|---|---|---|
| `0.2.0` | `0.2.0` | `0.2.0`, `0.2`, `0`, `latest` | stable |
| `0.2.0.post1` | `0.2.0.post1` | `0.2.0.post1`, `0.2`, `0`, `latest` | stable |
| `0.2.0b1`, `0.2.0rc1` | as tagged | the version, `beta` | pre-release (`pip install --pre`) |
| `0.2.0-beta.1` (old style) | `0.2.0b1` (normalised, with a notice) | `0.2.0b1`, `beta` | pre-release |
| `0.2.0.dev0`, `0.2.0+local`, `beta`, `0.2` | rejected | | |

Check a tag before you create it:

```bash
pdm run python scripts/release_version.py derive 0.2.0b1 --prerelease-flag true
```

What the workflow does, in order. Nothing is published unless every earlier step passed:

1. **version:** derives the one version both artifacts get (`scripts/release_version.py`). Fails on
   an unpublishable tag, on a pre-release checkbox that does not match the version, and on a tag
   whose commit is not on `main`.
2. **ci-reuse:** looks for a green CI run of exactly the tagged commit: a push to `main` in which all
   four CI jobs passed, the integration suite included (`scripts/ci_gate.py`). If there is one, its
   result vouches for the commit and step 3 is skipped. The changelog commit of step 2 above always
   runs the full CI, so tagging it once that CI is green is the fast path: the release then takes a
   few minutes instead of about 25. No such run (an intermediate commit, a docs-only push whose run skipped the integration job, a
   cancelled run) or an API error means the full CI runs.
3. **ci:** otherwise runs the whole [CI workflow](../.github/workflows/ci.yml) on the tagged commit:
   lint, types, unit and contract tests, secret scan, image smoke test and the integration suite.
4. **build-dist:** builds sdist and wheel with the locked `pdm-backend` and checks that the file names
   carry the derived version and that the sdist contains no tests (the integration fixtures hold
   public test keys).
5. **publish-pypi** and **publish-image:** run in the GitHub environment `release`, the only jobs that
   read the publish credentials. The image is built from a git context of the tagged commit, without
   build cache, and the build fails if the installed package does not report the derived version.

Re-running a failed release run is safe: PyPI skips files that already exist, and the image tags are
pushed again.

### Maintainer actions (repository and registry settings)

These need owner rights and no workflow does them. Until they are done the workflow still runs, but
the guarantees above are weaker: anyone with write access can create a release, and the publish
secrets are readable by every workflow run.

- [ ] **Protect `main`:** require a pull request and the checks `lint · type · test`,
      `secret scan (gitleaks)`, `image build · smoke test` and
      `integration (live Synapse · MAS · Authentik)`; disallow force pushes (after the history
      scrub, review session S16).
- [ ] **Protect tags:** a tag ruleset for `*` that restricts creation, update and deletion to
      maintainers.
- [ ] **Environment `release`:** create it (Settings > Environments) with a required reviewer and,
      optionally, deployment restricted to tags. Move `DOCKERHUB_DZD_USERNAME`,
      `DOCKERHUB_DZD_PASSWORD` and `PYPI_API_TOKEN` from the repository secrets into it and delete
      the repository-level copies. The workflow already uses this environment; if it does not exist,
      GitHub creates it without protection on the first run.
- [ ] **Actions settings** (Settings > Actions > General): workflow permissions "Read repository
      contents"; untick "Allow GitHub Actions to create and approve pull requests"; consider
      "Require actions to be pinned to a full-length commit SHA".
- [ ] **Docker Hub token:** `DOCKERHUB_DZD_PASSWORD` should be an access token limited to Read &
      Write on `dzdde/onbot` only, not an account password or an organisation-wide token.
- [ ] **PyPI Trusted Publishing:** on PyPI, add a Trusted Publisher for project `onbot` (owner
      `DZD-eV-Diabetes-Research`, repository `matrix-synapse-authentik-onbaording-bot`, workflow
      `release.yml`, environment `release`). Then delete `PYPI_API_TOKEN` in GitHub and revoke it on
      PyPI. No workflow change is needed: without the secret, the publish step uses Trusted
      Publishing and also uploads PEP 740 attestations. Until then the token keeps working; if one
      must stay, scope it to the `onbot` project.
- [ ] **Yank `0.1.0.dev0` on PyPI:** it sorts above every stable release, so
      `pip install --pre onbot` installs that July 2026 test build.
- [ ] **Decide about the stale `beta` Docker tag:** it still points at `0.0.1-beta.3`. Either delete
      it on Docker Hub (the next pre-release recreates it) or leave it until the next pre-release
      moves it.
