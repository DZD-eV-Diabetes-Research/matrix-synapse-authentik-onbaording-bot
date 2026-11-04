# 02 Supply chain, packaging, CI/release, git history

Reviewer area: Dockerfile, `.dockerignore`, `.github/workflows/*`, `pyproject.toml`, lockfiles,
pre-commit, helper scripts, published artifacts (Docker Hub `dzdde/onbot`, PyPI `onbot`) and a full git
history secret scan. Baseline commit `2c2e3a1` (= tag `0.0.11`).

**Summary.** The public git history still holds two real Matrix access tokens (the bot's and a human
admin's), reachable from `main` and every release tag; whether they were ever revoked is unconfirmed.
Eleven stable versions have been published to Docker Hub and PyPI although the CHANGELOG says the first
release is blocked on exactly that clean-up, and the release workflow is not gated on CI (0.0.1 shipped
from a red build, 0.0.3 from a `[skip ci]` commit). The secret scan that Phase 1 marks as done does not
run anywhere: the CI job was removed and the pre-commit config cannot even run. The image itself is in
decent shape (multi-stage, non-root, digest-pinned, verified version label), but the pin is five months
stale with no update automation.

| ID | Severity | Title |
|---|---|---|
| SUP-01 | critical | Two real Matrix access tokens in public git history, reachable from `main` and all tags |
| SUP-02 | high | Release pipeline is not gated on CI, review or protected refs |
| SUP-03 | medium | No working secret scan: CI job removed, pre-commit config broken |
| SUP-04 | medium | Workflow token, action pinning and publisher credential hardening |
| SUP-05 | medium | CI checks out `github.head_ref`: fork PRs test the wrong code, auto-format commits skip CI |
| SUP-06 | medium | Image version comes from a different source than the PyPI version and breaks for PEP 440 tags |
| SUP-07 | medium | Base image digest is 5 months stale and nothing will ever update it |
| SUP-08 | low | Image build is not reproducible and drops the lockfile hashes |
| SUP-09 | low | HEALTHCHECK probes upstreams with real credentials every 60 s and never checks the bot |
| SUP-10 | low | Python runs as PID 1 without a SIGTERM handler outside the reconcile loop |
| SUP-11 | low | Two lockfiles: `uv.lock` is unused and has drifted; CI does not enforce `pdm.lock` |
| SUP-12 | low | Stray and stale published artifacts (`0.1.0.dev0` on PyPI, Docker `beta` tag) |
| SUP-13 | low | Unbounded runtime dependency ranges for PyPI installs |
| SUP-14 | low | `provision-bot-account.sh` exposes the admin token (argv, terminal) and never revokes old ones |
| SUP-15 | low | Integration stack publishes known admin secrets on all interfaces; fixtures ship in the sdist |
| SUP-16 | low | `build_dev_env.sh` leaves `set -e` in the caller's interactive shell |
| SUP-17 | low | Local `docker build` bakes untracked files from `onbot/` into the image |
| SUP-18 | info | Deployment docs: `:latest`, config file readability for uid 10001, no hardening flags |
| SUP-19 | info | Docs and plan drift about releases and Phase 1 |

Checked and found clean: no `pull_request_target`; no `${{ }}` expression is interpolated into any
`run:` script (release name/body/branch are never used in shell, so no script injection); the release
jobs check out the tag's commit; the published image `dzdde/onbot:0.0.11` runs as uid 10001, reports
`onbot 0.0.11`, contains only the 19 locked runtime packages plus pip, and no config file;
`.dockerignore` excludes `config*.yml`, `*.env`, `.git`, tests and docs; `pdm lock --check` passes;
the PyPI wheel contains only `onbot/`.

---

## Critical

### SUP-01 Two real Matrix access tokens in public git history, reachable from `main` and all tags

- **Severity:** critical
- **Category:** security
- **Location:** history: `onbot/matrix_api_client.py` (b16af39, 2023-06-02), `onbot/test.py` (4a7488f, 2023-06-09), `onbot/readme_feat_room_avatar.txt` (6f7a00c, 2024-08-13); also any image built from the 2023 `dockerfile` (323a408, `COPY . .`)
- **Status:** confirmed (gitleaks v8.21.2 in a throwaway container over `--all`, plus a custom blob grep over every commit; values redacted at the pipe, never written to disk). Validity of the tokens was deliberately NOT tested.
- **Effort:** S for revocation (maintainer), M for the scrub

Scan headline: gitleaks reports 13 hits. 3 hits are 2 distinct real credentials, 10 are integration
test fixtures (see SUP-15). A second, pattern-based scan of every blob in all 78 commits found nothing
beyond those (no `mct_` token, no Authentik key, no password with a real value).

| # | Kind | Redacted | Where | Introduced, removed |
|---|---|---|---|---|
| 1 | Synapse access token of `@dzd-bot:dzd-ev.org` (base64 localpart decodes to `dzd-bot`) | `syt_...` (len 42) | `onbot/matrix_api_client.py:46`, `onbot/test.py:12,18,31` (same token, also as `Bearer ...`) | added b16af39 (2023-06-02); present in 34 commits up to 180c076; deleted in 89cbe64 (2026-06-19) |
| 2 | Synapse access token of the human user `@tim.bleimehl` (curl example with `authorization: Bearer`) | `syt_...` (len 48) | `onbot/readme_feat_room_avatar.txt:47` | added 6f7a00c (2024-08-13); deleted 6815d86 (2024-08-26) |
| 3 | Test fixtures: MAS EC + RSA private keys, MAS encryption secret, MAS to Synapse shared secret, 3 OAuth client secrets | `----...` (len 272, 1919); `fdae`, `7ad6`, `557e`, `04ca`... (len 64) | `tests/integration/stack/{mas/config.yaml,synapse/homeserver.yaml}`, `tests/integration/stack_api.py` | 3c69b3d (2026-06-23), still in HEAD by design |

Reachability: every commit above is an ancestor of `main`, `origin/main` and all 14 tags (`0.0.1-beta.1`
through `0.0.11`, `git tag --contains b16af39` lists all of them). The repository is public since
2023-05-24 (`gh api`: `visibility: public`), so the tokens have been world-readable for about 3 years
(token 1) and 2 years (token 2). Removing the files in 89cbe64 changed nothing about exposure.

Corrections to the battle plan: `config.dev.yml` and `get_access_token.sh` were never committed
(`git log --all -- <path>` is empty, both are gitignored), and every committed `config.yml` version only
holds placeholders (`xxx`, `null`). So the Authentik `api_key` and the login passwords named in Phase 1
are not in git history; they exist only on the maintainer's disk. The leaked items are the two `syt_`
tokens above.

Why critical: token 1 belongs to the bot, which in the legacy design used the Synapse admin API, and
token 2 is a personal session of the homeserver operator. If the homeserver was migrated to MAS with
`syn2mas`, existing Synapse access tokens are imported as compatibility sessions, so a 2023 token can
still be live today unless the session was explicitly ended. Phase 1 says rotation is still open.

Also note: the 2023 `dockerfile` (323a408 to 7518f9b) did `COPY . .` with no `.dockerignore`, so any
image built from it in that period contains `onbot/test.py` with token 1. Docker Hub `dzdde/onbot` only
carries 2026 tags now (checked via the public Hub API), but other registries or old local images may.

Fix: revocation first (it is the only real fix for a public leak), then the history scrub as hygiene.
Both are maintainer-only, see "Maintainer actions" A1 to A4.

---

## High

### SUP-02 Release pipeline is not gated on CI, review or protected refs

- **Severity:** high
- **Category:** security
- **Location:** `.github/workflows/release.yml:38-136`; repo settings
- **Status:** confirmed (`gh run list`, `gh api` read-only)
- **Effort:** S (workflow) plus maintainer settings

`release.yml` triggers on `release: published` and immediately builds and pushes `latest` to Docker Hub
and uploads to PyPI. Nothing ties it to a green CI run, and the repository has no guard rails around it:

- `main` is not protected (`GET branches/main/protection` returns 404) and there are no rulesets
  (`GET rulesets` returns `[]`), so tags and releases can point at any commit, including one pushed
  directly to `main` a minute earlier or on an unmerged branch.
- No deployment environment exists (`GET environments` is empty), so `DOCKERHUB_DZD_*` and
  `PYPI_API_TOKEN` are plain repository secrets readable by any workflow on any branch pushed by anyone
  with write access.
- The release jobs run no tests themselves.

This has already happened twice:

- `0.0.1` (published 2026-07-08 14:15 from `be0f6a7`): the CI run for that commit failed
  ("Ruff format check" step). The image and PyPI 0.0.1 were published anyway.
- `0.0.3` (from `4ee506e`, message "add tests for new tick design [skip ci]"): no CI run exists for
  that SHA at all.

Five releases (0.0.7 to 0.0.11) went out within 2 hours on 2026-08-04, each a few minutes after its
push. Docs tell operators to run `dzdde/onbot:latest` (SUP-18), and the bot holds Synapse admin, MAS
admin and Authentik credentials and can deactivate and erase accounts. A broken or tampered `latest`
therefore reaches production on the next pull with no human check.

Separately, the CHANGELOG and `release.yml` header say no release may be published before Phase 1 is
done; 11 stable versions were published regardless (see SUP-19).

Fix sketch: (1) add a `verify` job to `release.yml` that runs the quality + integration suites on the
tag SHA, and make both publish jobs `needs: verify`; or, cheaper, a first step that queries the check
runs of `github.sha` and fails unless `quality` and `integration` succeeded. (2) Put both publish jobs
in a `release` environment with a required reviewer and move the publish secrets there. (3) Protect
`main` and restrict tag creation (maintainer action A5).

---

## Medium

### SUP-03 No working secret scan: CI job removed, pre-commit config broken

- **Severity:** medium
- **Category:** security
- **Location:** `.github/workflows/ci.yml` (no scan job); `.pre-commit-config.yaml:5-15`; `docs/project/BATTLE_PLAN.md:191,197`
- **Status:** confirmed (git history of `ci.yml`; ran ruff 0.8.4 via `uvx` against `pyproject.toml`)
- **Effort:** S

Phase 1 is ticked "Add pre-commit + gitleaks (pre-commit hook **and** CI job)" and Phase 2 "lint, typecheck,
unit + secret scan". Neither holds:

1. The CI `secrets` job (`gitleaks/gitleaks-action@v2`) was added in 89cbe64 and silently deleted one
   commit later in 05c717b ("Phase 3"). The `a012a26` run in between failed. A likely cause is that
   `gitleaks-action@v2` needs a paid license key for organisation repos and that a full-history scan
   trips on SUP-01.
2. The pre-commit ruff hook is pinned to `v0.8.4`, which cannot parse `target-version = "py314"`
   ("unknown variant `py314`"). Every `git commit` with hooks installed fails at the ruff hook, which
   trains contributors to commit with `--no-verify`, disabling gitleaks too.
3. There is no `.gitleaksignore` or `.gitleaks.toml`, so the 10 fixture hits in `tests/integration/stack`
   (SUP-01 row 3) fire on any scan that covers them. Commit 3c69b3d added those fixtures, which an active
   gitleaks pre-commit hook would have blocked; the local pre-commit cache has no built gitleaks or ruff
   environment, consistent with the hooks never having run.
4. gitleaks `v8.21.2` is from late 2024.

Fix sketch: add a CI job that runs the gitleaks CLI directly (no license needed), e.g.
`docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:<pinned> git /repo --log-opts="--all"` with
`fetch-depth: 0`, and commit a `.gitleaksignore` listing the fingerprints of the fixture hits (and, until
the scrub is done, the historic SUP-01 hits, so new leaks still fail). Bump `ruff-pre-commit` to the
ruff version in `pdm.lock` (0.15.x) and gitleaks to current. Untick the Phase 1/2 boxes until then.

### SUP-04 Workflow token, action pinning and publisher credential hardening

- **Severity:** medium
- **Category:** security
- **Location:** `.github/workflows/ci.yml:16-17,41-44,58-75`; `.github/workflows/release.yml:47-136`; repo Actions settings
- **Status:** confirmed (workflow files; `gh api .../actions/permissions/workflow` returns `default_workflow_permissions: write`, `can_approve_pull_request_reviews: true`; `actions/permissions` returns `allowed_actions: all`, `sha_pinning_required: false`)
- **Effort:** S

Individually small, together they mean one compromised dependency or action can push to `main` and
ride the next release:

- `ci.yml` has no top-level `permissions:`. The `integration` job therefore gets the repository default,
  which is **write** for all scopes, while it installs every dev dependency, runs testcontainers and
  pulls six third-party images. `quality` explicitly asks for `contents: write`. `actions/checkout`
  persists that token in `.git/config`, so any code executed in those jobs (a pytest plugin, a
  compromised testcontainers release) can `git push` to the unprotected `main` on push events and
  same-repo PRs.
- Every action is referenced by a mutable tag (`actions/checkout@v4`, `pdm-project/setup-pdm@v4`,
  `stefanzweifel/git-auto-commit-action@v5`, `docker/*@v3/v5/v6`), and `pypa/gh-action-pypi-publish@release/v1`
  is a **branch**. Third-party actions run in the same job as the PyPI and Docker Hub credentials.
- PyPI uses a long-lived `PYPI_API_TOKEN` (last updated 2024-06-21; the workflow comment calls it
  "org-wide", so it is likely account-scoped rather than project-scoped) passed as `password:`.
  `id-token: write` is granted "for PyPI trusted publishing" but never used for it. With Trusted
  Publishing, the action would also generate PEP 740 attestations; with a token it does not.
- `provenance: true` / `sbom: true` produce BuildKit attestations (min-mode provenance) attached to the
  image index, unsigned. `id-token: write` in `publish-image` is unused (there is no
  `actions/attest-build-provenance` or cosign step), so the comment "for build provenance attestation"
  is inaccurate.
- `setup-pdm` and buildx use the GHA cache (`cache: true`, `cache-from: type=gha`) in the release
  jobs; caches written by any `main` run feed the release build. Low risk on its own, but it widens what
  a single compromised CI run can influence.

Fix sketch: `permissions: contents: read` at the top of both workflows, with per-job escalation only
where needed; drop the auto-commit (SUP-05) so `quality` can be read-only; pin all actions to full
commit SHAs (Dependabot keeps them fresh, SUP-07); switch PyPI to Trusted Publishing and delete the
token; either add `actions/attest-build-provenance` or remove the unused `id-token` grant; disable the
GHA cache in release jobs. Repo-level settings are maintainer action A5.

### SUP-05 CI checks out `github.head_ref`: fork PRs test the wrong code, auto-format commits skip CI

- **Severity:** medium
- **Category:** bug
- **Location:** `.github/workflows/ci.yml:19-24,36-44`
- **Status:** confirmed (by `actions/checkout` semantics: `ref` resolves against `github.repository`, the base repo; not executed)
- **Effort:** S

`ref: ${{ github.head_ref }}` asks checkout for a branch **name** in the base repository. For a PR from
a fork, that branch lives in the fork:

- If the fork's branch name also exists upstream (the common case: a fork PR from its own `main`), CI
  checks out upstream `main`, tests it, and reports green on the PR. The contributor's code is never
  tested by `quality`. (The `integration` job does not set `ref`, so it tests the merge ref; the two
  jobs then test different code.)
- Otherwise checkout fails and the PR is red for an unrelated reason.

For same-repo PRs it checks out the branch tip at job start, not `github.sha`, so the status can be
attached to a commit that was not the one tested if a push races the run.

`git-auto-commit-action` then pushes "style: auto-format with ruff" with `GITHUB_TOKEN`. Pushes made with
`GITHUB_TOKEN` do not trigger workflows, so on `main` the auto-format commit becomes HEAD without any CI
run, and a maintainer who tags HEAD releases an untested commit (feeds SUP-02). On fork PRs the push is
rejected (read-only token).

Fix sketch: remove `ref:` and the auto-commit step; run `ruff format --check .` and fail. Formatting is
already enforced locally by the pre-commit hook once SUP-03 is fixed.

### SUP-06 Image version comes from a different source than the PyPI version and breaks for PEP 440 tags

- **Severity:** medium
- **Category:** bug
- **Location:** `.github/workflows/release.yml:63-75,90-93,108-125`; `docs/development.md:67`; `Dockerfile:39-44`; `pyproject.toml:43-45`
- **Status:** confirmed (read `docker/metadata-action` v5 `src/meta.ts:178`: strict `semver.valid`; built the wheel locally with `PDM_BUILD_SCM_VERSION=beta`, which produced `onbot-0.0.0`)
- **Effort:** S

PyPI's version comes from pdm-backend reading the checked-out git tag. The image's version comes from
`steps.meta.outputs.version`, i.e. whatever `docker/metadata-action` made of the tag. The two diverge:

- The docs (`development.md:67`, `release.yml` header) say to use PEP 440 pre-release tags such as
  `0.2.0b1`. metadata-action rejects that as not strict semver, skips all `type=semver` rules, and the
  only remaining tag is `type=raw,value=beta`, so `outputs.version` becomes `beta`. The build arg is then
  `PDM_BUILD_SCM_VERSION=beta`; pdm-backend silently ignores the invalid value and falls back to
  `fallback_version = "0.0.0"`. Result: a beta image with no version tag at all, whose
  `onbot --version` says `0.0.0`, while PyPI correctly gets `0.2.0b1`.
- Releases so far used semver-style tags (`0.0.1-beta.1`), which works for Docker but is the
  non-canonical PEP 440 spelling; PyPI normalises it to `0.0.1b1`. So the documented and the working
  scheme differ.
- An empty or non-version tag behaves the same: `0.0.0` in the image, no error.

Fix sketch: derive one version once, in a first job, from `github.ref_name` with
`packaging.version.Version` (fail on invalid), and pass it as a job output to both the Docker build arg
and a `PDM_BUILD_SCM_VERSION` env for `pdm build`; generate the Docker tags with `type=raw` from that
output. In the Dockerfile, fail the build when `PDM_BUILD_SCM_VERSION` is set but produces `0.0.0`
(e.g. `python -c` check after `pip install`).

### SUP-07 Base image digest is 5 months stale and nothing will ever update it

- **Severity:** medium
- **Category:** security
- **Location:** `Dockerfile:16-17`; `.github/` (no `dependabot.yml`, no Renovate config)
- **Status:** confirmed (`docker buildx imagetools inspect`)
- **Effort:** S

The pinned digest `python@sha256:a705...` is `python:3.14.6-slim-bookworm`, created 2026-06-11. Today's
`python:3.14-slim-bookworm` is 3.14.10, created 2026-10-28, and the default `3.14-slim` has moved to
Debian trixie. Every image published since July, including 0.0.11, ships CPython 3.14.6 plus the June
Debian package set. Digest pinning is right, but without an updater it turns into "never patched". The
same applies to the GitHub Actions versions and the Python lockfile (`pdm.lock` is only refreshed by
hand).

Fix sketch: add `.github/dependabot.yml` with `docker` (Dockerfile and the test compose file),
`github-actions` and `pip`/`uv` ecosystems (Dependabot does not understand `pdm.lock` natively;
Renovate does, which is another reason to choose one lockfile, SUP-11). Consider a scheduled weekly
rebuild of the current release tag so base fixes reach users between releases. Decide bookworm vs trixie
deliberately.

---

## Low

### SUP-08 Image build is not reproducible and drops the lockfile hashes

- **Severity:** low
- **Category:** security
- **Location:** `Dockerfile:27,32,37,44`
- **Status:** confirmed (by reading; `pdm.lock` has 363 sha256 hashes)
- **Effort:** S

The header claims a reproducible build, but: `pip install pdm` installs whatever pdm is newest at build
time; `pdm export --no-hashes` throws away the hashes that `pdm.lock` carries, so the venv install does
not verify artifacts; `pip install --no-deps .` builds in an isolated env that pulls the newest
`pdm-backend` (unpinned in `[build-system]`). Each of those runs third-party code from PyPI in the build
with no pin. The runtime image also keeps `pip` in `/opt/venv` (seen in the 0.0.11 image), which the
bot never needs.

Fix sketch: pin pdm (`pip install pdm==2.27.0`) or use `pdm export` on the CI host; export with hashes
and install with `--require-hashes`; build the onbot wheel in the builder with `--no-build-isolation`
after installing a pinned `pdm-backend`, or pin it in `[build-system] requires`; create the venv with
`--without-pip` and install via the builder's pip (`pip --python /opt/venv/bin/python`).

### SUP-09 HEALTHCHECK probes upstreams with real credentials every 60 s and never checks the bot

- **Severity:** low
- **Category:** robustness
- **Location:** `Dockerfile:74-75`; `onbot/healthcheck.py:77-135`
- **Status:** confirmed (code reading)
- **Effort:** M

Each healthcheck is a fresh process that loads the config, builds new clients and sends 3 or 4
authenticated requests (Synapse whoami, Synapse admin `v2/users`, Authentik `core/users/`, MAS admin).
With `mas_admin` configured, every run performs a new `client_credentials` exchange at MAS, and a second
one when the Matrix side also uses `synapse_server.oauth2`: up to 2880 access tokens minted per day that
MAS stores until expiry. The probe says nothing about the bot itself: a wedged reconcile loop or a dead
sync pump stays "healthy" as long as the upstreams answer, while an Authentik outage marks the bot
unhealthy, and under Swarm or an autoheal sidecar restarts it for no gain (the bot already retries).

Fix sketch: make the container health reflect liveness of the bot process (e.g. the engine and sync pump
touch a heartbeat file or expose a tiny local endpoint; the healthcheck checks its age). Keep
`onbot healthcheck` as an operator command for dependency checks, not the `HEALTHCHECK`. Cross-reference
`05-runtime-clients.md` for the healthcheck code itself.

### SUP-10 Python runs as PID 1 without a SIGTERM handler outside the reconcile loop

- **Severity:** low
- **Category:** robustness
- **Location:** `Dockerfile:77-78`; `onbot/reconciler/engine.py:104,123-129`; `onbot/app.py:237-262`
- **Status:** confirmed (in `dzdde/onbot:0.0.11`, a Python process as PID 1 took 10.1 s to `docker stop`, i.e. SIGTERM was ignored and SIGKILL followed)
- **Effort:** S

The exec-form `ENTRYPOINT ["onbot"]` makes Python PID 1, and the kernel drops signals with default
disposition for PID 1. The only SIGTERM handler is installed by `ReconcileEngine.run()`. During service
startup (before the engine starts), and for the whole of `reconcile-once` and `broadcast`, `docker stop`
waits 10 s and then SIGKILLs mid-operation (e.g. halfway through a broadcast fan-out).

Fix sketch: `STOPSIGNAL SIGINT` in the Dockerfile (Python's default SIGINT handler raises
`KeyboardInterrupt`, which works as PID 1), or install the handler in `cli.main` for every command, or
add `tini` as entrypoint.

### SUP-11 Two lockfiles: `uv.lock` is unused and has drifted; CI does not enforce `pdm.lock`

- **Severity:** low
- **Category:** maintainability
- **Location:** `uv.lock`; `pdm.lock`; `.github/workflows/ci.yml:31-32,57-58`; `Dockerfile:31-32`
- **Status:** confirmed (version diff of both lockfiles; `pdm lock --check` exit 0)
- **Effort:** S

`pdm.lock` is authoritative: CI (`pdm install --dev`), the Dockerfile (`pdm export --prod`) and the
battle plan ("PDM chosen over uv") all use it, and it is in sync with `pyproject.toml`. `uv.lock` was
committed in e71bfcf alongside a pdm.lock update, is referenced nowhere, and already differs on 20 of 54
packages, including the runtime dependency `pydantic-settings` (pdm 2.14.1, uv 2.14.2), `certifi`,
`anyio` and `typing-extensions`. Anyone running `uv sync` gets a different set than CI and the image,
and update bots will happily bump the wrong file.

Separately, `pdm install` in CI re-resolves when the lock is stale instead of failing, so a
`pyproject.toml` change without a re-lock makes CI test freshly resolved versions while the image still
exports the stale lock.

Fix sketch: delete `uv.lock` (and add it to `.gitignore`), or switch fully to uv and delete `pdm.lock`.
In CI use `pdm install --dev --frozen-lockfile` and add `pdm lock --check`.

### SUP-12 Stray and stale published artifacts (`0.1.0.dev0` on PyPI, Docker `beta` tag)

- **Severity:** low
- **Category:** bug
- **Location:** PyPI `onbot`, Docker Hub `dzdde/onbot:beta`
- **Status:** confirmed (PyPI JSON API and Docker Hub tags API, read-only)
- **Effort:** S

PyPI holds `0.1.0.dev0`, uploaded 2026-07-08 06:17 by the `0.0.1-beta.2` release run while `pyproject.toml`
still had a hard-coded `version = "0.1.0.dev0"` (5df24a6). Under PEP 440 it sorts **above** every stable
release, so `pip install --pre onbot`, `uv pip install --prerelease=allow onbot` or any resolver allowing
pre-releases installs that July test build (which also lacks the `markdown-it-py` dependency). It is not
yanked. On Docker Hub the `beta` tag still points at `0.0.1-beta.3` from 2026-07-08, older than every
stable release, so anyone following the documented beta channel runs the oldest code.

Fix sketch: yank `0.1.0.dev0` on PyPI (maintainer action A7); either keep `beta` moving (also tag stable
releases as `beta`) or delete it and document that `beta` only exists during a pre-release.

### SUP-13 Unbounded runtime dependency ranges for PyPI installs

- **Severity:** low
- **Category:** robustness
- **Location:** `pyproject.toml:21-28`
- **Status:** plausible (no breaking upstream release observed yet)
- **Effort:** S

The image is locked, but the PyPI package declares only lower bounds (`httpx>=0.27`, `pydantic>=2.7`,
`pydantic-settings>=2.3`, `tenacity>=9.0`, ...). onbot is an application, not a library, and it uses
httpx internals-adjacent behaviour (base client, retries) and pydantic-settings source customisation.
A `pip install onbot` after httpx 1.0 or pydantic 3 resolves to an untested combination with no change on
our side. The lower bounds are also untested: CI only ever runs the locked (newest) versions.

Fix sketch: cap at the next major for the libraries the code leans on (`httpx>=0.28,<1`,
`pydantic>=2.7,<3`, `pydantic-settings>=2.3,<3`), or document the image as the only supported install
path and publish a constraints file with each release.

### SUP-14 `provision-bot-account.sh` exposes the admin token (argv, terminal) and never revokes old ones

- **Severity:** low
- **Category:** security
- **Location:** `scripts/provision-bot-account.sh:125-126,173,185-186,200-201,211-228`
- **Status:** confirmed (code reading)
- **Effort:** S

- Line 185 puts the freshly minted admin-scoped token on a command line
  (`docker compose exec ... curl -H "Authorization: Bearer ${TOKEN}"`), visible in `ps` on the host
  (the `docker compose` process) and inside the Synapse container for the duration of the call.
- In the default (non-quiet) mode the full token is printed to stderr in the config snippet
  (lines 211-224), so it lands in terminal scrollback and in any log the run is captured into.
- `--write-token-to` writes with the process umask and only then `chmod 600` (lines 200-201): a short
  window where the file is world-readable under a 022 umask.
- Lines 125-126 `source` the stack's `.env` as shell, executing any command substitution in it and
  mis-parsing compose-style values with spaces or `$`.
- `--reissue-only` mints a new compatibility token and "revokes nothing else": every rotation leaves the
  previous admin token valid indefinitely. This is directly relevant to SUP-01's revocation.

Fix sketch: pass the header via stdin (`curl -H @-`) or run whoami from the host against the published
port with `--config` from a here-doc; print the token only with `--quiet`/`--write-token-to` and mask it
in the snippet; create the file with `umask 077` in a subshell; read `.env` with a `grep`/`sed` key
lookup instead of sourcing; add a `--revoke-previous` option that ends the bot's other compatibility
sessions (`mas-cli manage kill-sessions` or the MAS admin API) after the new token is verified.

### SUP-15 Integration stack publishes known admin secrets on all interfaces; fixtures ship in the sdist

- **Severity:** low
- **Category:** security
- **Location:** `tests/integration/stack/docker-compose.yml:40-53,87-88,111-112`; `tests/integration/stack/mas/config.yaml:30-95`; sdist of every PyPI release
- **Status:** confirmed (compose file; downloaded the `onbot-0.0.11` sdist and listed it)
- **Effort:** S

The test stack maps `9000:9000` (Authentik), `8080:8080` (MAS) and `8008:8008` (Synapse) on `0.0.0.0`,
with a static Authentik bootstrap admin password and API token, static MAS admin client secret and MAS
signing keys, all committed and public. While the stack runs (and it stays up with `ONBOT_ITEST_KEEP=1`),
anyone on the same network can log in to the test Authentik as admin or mint MAS admin tokens. Docker's
port publishing bypasses host firewalls such as ufw. Harmless data, but a foothold on a developer
machine's network. The pdm-backend sdist also includes the whole `tests/` tree, so these keys are
redistributed on PyPI with every release, and they trip every secret scanner run over the package.

Fix sketch: bind to loopback (`"127.0.0.1:9000:9000"`) or let testcontainers pick random host ports;
generate the secrets per run in `conftest.py`; exclude `tests/integration/stack` from the sdist
(`[tool.pdm.build] excludes`); allowlist the fixture fingerprints in `.gitleaksignore` (SUP-03).

### SUP-16 `build_dev_env.sh` leaves `set -e` in the caller's interactive shell

- **Severity:** low
- **Category:** bug
- **Location:** `build_dev_env.sh:16,27`
- **Status:** confirmed (code reading; bash exits an interactive shell on error under `set -e`)
- **Effort:** S

The script must be sourced, and line 16 runs `set -eo pipefail` in the caller's shell. After
`source ./build_dev_env.sh`, the next failing command the developer types (a failing `grep`, a red
`pytest`) closes the terminal. Line 27 pipes an unpinned remote installer into `python3`
(`curl -sSL https://pdm-project.org/install-pdm.py | python3 -`) with no checksum. The closing hint also
says `./run_tests.sh` has "the coverage gate", which `run_tests.sh` explicitly does not.

Fix sketch: wrap the body in a function that runs in a subshell for the install steps and only does
`source .venv/bin/activate` in the caller, or save and restore `$-`; prefer `pipx install pdm==<ver>`.

### SUP-17 Local `docker build` bakes untracked files from `onbot/` into the image

- **Severity:** low
- **Category:** security
- **Location:** `Dockerfile:43-44`; `.dockerignore`
- **Status:** plausible (pdm-backend packages every file under the package directory; not reproduced)
- **Effort:** S

`COPY onbot ./onbot` followed by `pip install .` puts every file under `onbot/` into the wheel and thus
into `/opt/venv/.../site-packages`. CI builds from a clean checkout, but a maintainer building locally
(`docs/development.md:52`) ships whatever scratch files sit in the working tree. That is exactly how
SUP-01 token 1 entered the repo (`onbot/test.py`). `.dockerignore` only excludes top-level `config*.yml`.

Fix sketch: build the image from `git archive HEAD` or a wheel built in CI, or add `.dockerignore`
entries for `onbot/**/test*.py`, `onbot/**/*.txt`, `onbot/**/*.yml`; better, keep the gitleaks hook
working so scratch files are caught earlier.

---

## Info

### SUP-18 Deployment docs: `:latest`, config file readability for uid 10001, no hardening flags

- **Severity:** info
- **Category:** docs
- **Location:** `docs/deployment.md:11-46`
- **Status:** confirmed (docs reading)
- **Effort:** S

All examples use `dzdde/onbot:latest`, which given SUP-02 and the bot's destructive powers is the least
safe choice; recommend a pinned version or digest. The image runs as uid 10001, so a `config.yml` mounted
read-only from the host must be readable by that uid; an operator with a `0600` file owned by their own
user gets a permission error and the obvious fix (`chmod 644`) makes the secrets file world-readable on
the host. Document `chown 10001` or Docker/compose secrets. No example sets `read_only: true`,
`cap_drop: [ALL]` or `security_opt: [no-new-privileges:true]`, which this image supports as is (it writes
nothing locally).

### SUP-19 Docs and plan drift about releases and Phase 1

- **Severity:** info
- **Category:** docs
- **Location:** `CHANGELOG.md:9-10,85-88`; `.github/workflows/release.yml:31-32`; `docs/project/BATTLE_PLAN.md:186-191,197`
- **Status:** confirmed
- **Effort:** S

- CHANGELOG says "Versions are not yet published" and has only `[Unreleased]`, while 0.0.1 to 0.0.11 are on
  PyPI and Docker Hub. There is no per-version entry to tell users what changed.
- CHANGELOG and `release.yml` point to "the README" for the Phase 1 hand-off; the README no longer
  mentions it.
- BATTLE_PLAN Phase 1 attributes the Authentik key and passwords to committed files; they were never
  committed (SUP-01). The leaked tokens that matter are not named there.
- BATTLE_PLAN Phase 1/2 tick the CI secret scan, which was removed (SUP-03).

---

## Maintainer actions

Everything below needs repository, registry or homeserver owner rights. Nothing here was executed.
Order matters: revoke (A1 to A3) before scrubbing (A4); a scrub without revocation protects nothing,
because the history has been public for years.

**A1. Revoke the leaked Matrix tokens (SUP-01).** Do not test them. End the sessions instead:
- On the MAS-backed homeserver: `mas-cli manage kill-sessions dzd-bot` and
  `mas-cli manage kill-sessions tim.bleimehl` (run inside the `mas` container), then reissue the bot's
  token with `scripts/provision-bot-account.sh --reissue-only` and update the deployed config. Ending all
  sessions for `tim.bleimehl` also logs out that user's devices; alternatively find and end only the
  compatibility session created before 2024-08-26 in the MAS admin UI or admin API.
- If any Synapse for this server still runs without MAS, delete the users' devices with the Synapse
  admin API (`GET /_synapse/admin/v2/users/<mxid>/devices`, then
  `POST /_synapse/admin/v2/users/<mxid>/delete_devices`), which invalidates their access tokens.
- Confirm the result by listing the users' sessions (MAS admin API `GET /api/admin/v1/compat-sessions?filter[user]=...`)
  and checking that none predates the revocation.

**A2. Check for misuse.** Before or right after A1, look at where those sessions were used from:
Synapse `GET /_synapse/admin/v1/whois/@dzd-bot:dzd-ev.org` (and for `@tim.bleimehl`), MAS session
`last_active_ip`, and the Synapse access log for the bot from unexpected IPs since 2023.

**A3. Rotate the remaining Phase 1 items anyway.** The Authentik `api_key` and the `@admin`/`@dzd-bot`
passwords live in the untracked `config.dev.yml` and `get_access_token.sh`; they are not in git, but
rotate them if those files were ever shared, synced or backed up outside the workstation. Also revoke
any compatibility tokens left over from previous `--reissue-only` runs (SUP-14).

**A4. History scrub (hygiene, after A1).** Plan, with a fresh mirror and literal replacements so no regex
touches unrelated code:

```bash
# 0. Announce a freeze; nobody pushes during the rewrite.
git clone --mirror git@github.com:DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot.git onbot-scrub.git
cd onbot-scrub.git

# 1. Write the two token values (from `git grep -hoE 'syt_[A-Za-z0-9_]+' $(git rev-list --all) | sort -u`,
#    the len-42 and len-48 ones) into a file OUTSIDE any repo, one per line, as:
#       <token-value>==>REDACTED_TOKEN
#    (a literal match also covers the "Bearer <token-value>" occurrences).
git filter-repo --replace-text ../onbot-replacements.txt \
                --invert-paths --path onbot/test.py --path onbot/readme_feat_room_avatar.txt

# 2. Verify: only the test fixtures (tests/integration/stack, stack_api.py) may remain.
docker run --rm -v "$PWD:/repo:ro" zricethezav/gitleaks:v8.21.2 git /repo --log-opts=--all

# 3. Push the rewritten branches and all 14 tags (filter-repo drops the origin remote).
git remote add origin git@github.com:DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot.git
git push --force --mirror origin
shred -u ../onbot-replacements.txt
```

Consequences to plan for: every commit SHA changes, so all 14 tags and GitHub Releases move to new SHAs;
the `org.opencontainers.image.revision` label in published images and the sdists on PyPI then reference
commits that no longer exist on any branch. GitHub keeps the old objects reachable by SHA and in cached
views until GitHub Support purges them (open a support request "remove sensitive data" with the old
commit SHAs b16af39, 4a7488f, 6f7a00c and their descendants). Every existing clone (including this
workstation) must be re-cloned, not pulled. The repo has 0 forks and 0 PRs today, which keeps this cheap.

**A5. GitHub repository settings (SUP-02, SUP-04).**
- Protect `main`: require a PR, require the `lint · type · test` and `integration` checks, disallow force
  pushes (after A4).
- Add a tag ruleset for `*` that restricts creation and deletion to maintainers.
- Create an environment `release` with a required reviewer, move `DOCKERHUB_DZD_USERNAME`,
  `DOCKERHUB_DZD_PASSWORD` and (until A7) `PYPI_API_TOKEN` into it, and delete the repository-level copies.
- Settings > Actions > General: workflow permissions "Read repository contents", untick "Allow GitHub
  Actions to create and approve pull requests", and consider "Require actions to be pinned to a full-length
  commit SHA".

**A6. Docker Hub credential.** `DOCKERHUB_DZD_PASSWORD` (updated 2026-04-08) should be an organisation
access token limited to Read & Write on `dzdde/onbot` only, not an account password or a token that can
push to every `dzdde/*` repository. Check old registries (other Docker Hub repo names, GHCR from the
0.0.1-beta.1 pipeline, any 2023/2024 builds from the old `dockerfile`) for images containing
`onbot/test.py` and delete them.

**A7. PyPI.** Configure a Trusted Publisher for project `onbot` (owner `DZD-eV-Diabetes-Research`, repo
`matrix-synapse-authentik-onbaording-bot`, workflow `release.yml`, environment `release`), then delete
`PYPI_API_TOKEN` on PyPI and in GitHub. If a token must stay, make it project-scoped to `onbot`. Yank
`0.1.0.dev0` (SUP-12).
