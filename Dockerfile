# syntax=docker/dockerfile:1
#
# Onbot production image (BATTLE_PLAN.md §5 Phase 8).
#
#   * Multi-stage: a builder resolves the *locked, prod-only* deps into a venv; the final stage
#     copies just that venv onto a slim base — no PDM, no build tools, no dev/test deps.
#   * Base image pinned by digest (reproducible; update the digest deliberately, not implicitly).
#   * Runs as a non-root user.
#   * No crypto stack (libolm): the bot operates outside encrypted rooms (ADR-0009).
#   * HEALTHCHECK calls `onbot healthcheck`, which probes the configured Synapse/MAS/Authentik
#     endpoints with the real credentials and exits non-zero when a dependency is unhealthy.
#
# Config is provided at runtime — never baked in (no secrets in the image). Mount a config file and
# point ONBOT_CONFIG_FILE_PATH at it, or supply every setting via ONBOT_* env vars. See README.md.

# python:3.14-slim-bookworm — pin by digest; refresh with `docker pull` + `docker inspect`.
ARG PYTHON_IMAGE=python@sha256:a70519002c49552ea0a853de47599cf40479b001bd7a624f1112eaf44dcaccc7

# --- builder: resolve locked prod deps into /opt/venv ------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PDM_CHECK_UPDATE=false

RUN pip install --no-cache-dir pdm

WORKDIR /app

# Export the locked production dependency set, then install it into a clean venv. Copying only the
# lock/metadata first keeps this layer cached across source-only changes.
COPY pyproject.toml pdm.lock README.md ./
RUN pdm export --prod --no-hashes -o /tmp/requirements.txt

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Install the onbot package itself (no deps — they are already pinned above). The build context has
# no .git, so pdm-backend can't derive the version from SCM; the release workflow passes it here via
# --build-arg PDM_BUILD_SCM_VERSION=<tag>. Without it the package falls back to fallback_version.
ARG PDM_BUILD_SCM_VERSION
ENV PDM_BUILD_SCM_VERSION=${PDM_BUILD_SCM_VERSION}
COPY onbot ./onbot
RUN pip install --no-cache-dir --no-deps .

# --- runtime: slim image with just the venv + a non-root user --------------------------------
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="onbot" \
      org.opencontainers.image.description="Keep a Matrix (Synapse) homeserver in sync with Authentik and onboard new users." \
      org.opencontainers.image.source="https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ONBOT_CONFIG_FILE_PATH=/config/config.yml

# Non-root runtime user; /config is where an operator mounts the config file.
RUN useradd --create-home --uid 10001 onbot \
 && mkdir -p /config \
 && chown onbot:onbot /config

COPY --from=builder /opt/venv /opt/venv

USER onbot
WORKDIR /home/onbot

# Probe the configured dependencies. Generous start period: the bot's homeserver/IdP may still be
# coming up alongside it. Exits non-zero (unhealthy) if any dependency is unreachable/unauthorized.
HEALTHCHECK --interval=60s --timeout=20s --start-period=30s --retries=3 \
    CMD ["onbot", "healthcheck"]

ENTRYPOINT ["onbot"]
CMD ["run"]
