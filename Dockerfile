# Onbot production image (BATTLE_PLAN.md §5 Phase 8).
#
#   * Multi-stage: a builder installs the *locked, prod-only* deps into a venv; the final stage
#     copies just that venv onto a slim base. No PDM, no pip, no build tools, no dev/test deps.
#   * Reproducible from pdm.lock: every package (runtime deps and the pdm-backend that builds the
#     onbot wheel) is installed by exact version with the lock's sha256 hashes (--require-hashes),
#     wheels only. scripts/lock_requirements.py reads pdm.lock with the standard library, so PDM
#     itself is never installed here. The base image is pinned by tag and digest, tini by
#     checksum. Dependabot proposes base image updates (.github/dependabot.yml).
#   * Only package files reach the image: .dockerignore is an allowlist (Python files under onbot/,
#     the lock and the metadata). The release workflow builds from a git context of the tagged
#     commit, so untracked files never enter a published image (docs/development.md).
#   * Runs as a non-root user.
#   * No crypto stack (libolm): the bot operates outside encrypted rooms (ADR-0009).
#   * HEALTHCHECK calls `onbot healthcheck`, which reads the heartbeat file the running service
#     writes (reconcile loop + sync pump) and exits non-zero when it is stale. It makes no network
#     call and mints no token; `onbot healthcheck --deep` is the dependency probe for operators.
#   * Signals: tini is PID 1 and forwards SIGTERM to `onbot`, which shuts down gracefully within
#     the default 10 s of `docker stop` (see docs/deployment.md, "Stopping").
#
# Config is provided at runtime, never baked in (no secrets in the image). Mount a config file and
# point ONBOT_CONFIG_FILE_PATH at it, or supply every setting via ONBOT_* env vars. See README.md.

# --- builder: install the locked prod deps and the onbot wheel into /opt/venv -----------------
# Base image: python:3.14.x-slim-trixie (Debian 13) by tag and digest. Dependabot updates both
# (the same reference appears in the runtime stage below; keep the two identical).
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# tini as the static binary from the upstream release, verified by checksum. Fetched here so the
# runtime stage needs no apt (and does not depend on the Debian mirror state at build time).
ARG TINI_VERSION=v0.19.0
ARG TINI_SHA256_AMD64=c5b0666b4cb676901f90dfcb37106783c5fe2077b04590973b885950611b30ee
ARG TINI_SHA256_ARM64=eae1d3aa50c48fb23b8cbdf4e369d0910dfc538566bfd09df89a774aa84a48b9
RUN arch="$(dpkg --print-architecture)" \
 && case "$arch" in \
        amd64) sha="$TINI_SHA256_AMD64" ;; \
        arm64) sha="$TINI_SHA256_ARM64" ;; \
        *) echo "no tini checksum for architecture $arch" >&2; exit 1 ;; \
    esac \
 && python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
        "https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini-static-${arch}" /tmp/tini \
 && echo "${sha}  /tmp/tini" | sha256sum -c - \
 && chmod 0755 /tmp/tini

# Lock and metadata first, so the dependency layer stays cached across source-only changes.
COPY pyproject.toml pdm.lock README.md LICENSE ./
COPY scripts/lock_requirements.py scripts/release_version.py ./scripts/

# Build tools (pdm-backend, packaging) go into the builder's own Python; the runtime venv is created
# without pip and filled by the builder's pip, so it ends up with the runtime packages only.
RUN python scripts/lock_requirements.py build > /tmp/build-requirements.txt \
 && python scripts/lock_requirements.py default > /tmp/runtime-requirements.txt \
 && pip install --require-hashes --no-deps --only-binary=:all: -r /tmp/build-requirements.txt \
 && python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install \
        --require-hashes --no-deps --only-binary=:all: -r /tmp/runtime-requirements.txt

# The onbot package itself. The build context has no .git, so pdm-backend cannot derive the version
# from SCM; the release workflow passes it as --build-arg PDM_BUILD_SCM_VERSION=<version> (from
# scripts/release_version.py, the same value the PyPI build gets). Without it the package carries
# fallback_version (0.0.0), which is fine for a local build. When it is set, the build fails unless
# the installed package reports exactly that version.
ARG PDM_BUILD_SCM_VERSION
COPY onbot ./onbot
RUN pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/dist . \
 && pip --python /opt/venv/bin/python install --no-deps /tmp/dist/onbot-*.whl \
 && installed="$(/opt/venv/bin/python -c 'import importlib.metadata as m; print(m.version("onbot"))')" \
 && echo "onbot ${installed}" \
 && if [ -n "${PDM_BUILD_SCM_VERSION}" ]; then \
        python scripts/release_version.py verify --expected "${PDM_BUILD_SCM_VERSION}" --actual "${installed}"; \
    fi

# --- runtime: slim image with just the venv + a non-root user --------------------------------
FROM python:3.14.10-slim-trixie@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS runtime

LABEL org.opencontainers.image.title="onbot" \
      org.opencontainers.image.description="Keep a Matrix (Synapse) homeserver in sync with Authentik and onboard new users." \
      org.opencontainers.image.source="https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ONBOT_CONFIG_FILE_PATH=/config/config.yml

# Non-root runtime user; /config is where an operator mounts the config file. tini runs as PID 1:
# the kernel drops a signal to PID 1 that has no handler, and onbot can only install its handlers
# once the interpreter has imported it (about a second), so a `docker stop` in that window would
# otherwise wait for the SIGKILL. tini forwards the signal and reaps zombies.
RUN useradd --create-home --uid 10001 onbot \
 && mkdir -p /config \
 && chown onbot:onbot /config

COPY --from=builder /tmp/tini /usr/bin/tini
COPY --from=builder /opt/venv /opt/venv

USER onbot
WORKDIR /home/onbot

# Liveness from the heartbeat file (default /tmp/onbot-heartbeat.json, override with
# ONBOT_HEARTBEAT_FILE). The staleness limits are written into the file by the service itself, so the
# check needs no config and no network.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["onbot", "healthcheck"]

ENTRYPOINT ["/usr/bin/tini", "--", "onbot"]
CMD ["run"]
