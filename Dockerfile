# syntax=docker/dockerfile:1
#
# Omni Analyst v2 - API image.
#
# Stateless uvicorn process serving JSON. Migrations run in the app lifespan on
# startup, so this image is self-contained: bring it up pointed at a reachable
# Postgres and it will migrate then serve.
#
# Neutron is installed from the version and artifacts pinned in uv.lock.

# --------------------------------------------------------------------------- #
# Stage 1 - builder: resolve and install every dependency into a clean venv.   #
# --------------------------------------------------------------------------- #
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# - UV_LINK_MODE=copy:  copy files in, never hardlink across layers.
# - UV_COMPILE_BYTECODE precompiles .pyc so the runtime pays no import tax.
# - UV_PYTHON_DOWNLOADS=never: use the interpreter shipped in the base image.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
ARG NEUTRON_PACKAGE_VERSION
RUN uv sync --locked --no-dev --no-install-project \
 && test "$(/app/.venv/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("neutron-framework"))')" = "${NEUTRON_PACKAGE_VERSION}"

# Application source and migrations. We do NOT pip-install the project: the
# migrations loader (omni.db) finds migrations/ by walking up from this file
# layout (src/omni/db.py -> parents[2] -> /app -> /app/migrations), so the src
# tree must sit at /app/src at runtime and be importable via PYTHONPATH.
COPY src/    ./src/
COPY migrations/ ./migrations/

# --------------------------------------------------------------------------- #
# Stage 2 - runtime: slim, no build toolchain, no tests, no ui/, non-root.     #
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS runtime

ARG OMNI_REVISION
ARG NEUTRON_PACKAGE_VERSION

RUN printf '%s' "${OMNI_REVISION}" | grep -Eq '^[0-9a-f]{40}$' \
 && test -n "${NEUTRON_PACKAGE_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:${PATH} \
    OMNI_BUILD_REVISION=${OMNI_REVISION} \
    NEUTRON_PACKAGE_VERSION=${NEUTRON_PACKAGE_VERSION}

LABEL org.opencontainers.image.revision=${OMNI_REVISION} \
      com.omnianalyst.neutron.version=${NEUTRON_PACKAGE_VERSION}

# pg_dump (exact server major: the store is Postgres 17, and a client older
# than the server cannot dump it) for the Settings backup download. PGDG
# because bookworm's stock client is 15.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. uid 10001 avoids colliding with any host uid bind-mounted in.
RUN useradd --create-home --uid 10001 --shell /sbin/nologin omni

# The credential-key volume mounts here. Provisioned in the image so a fresh
# named volume (mounted empty, root-owned by default) still leaves uid 10001
# able to create credential.key inside it on first boot.
RUN install -d -m 0700 -o omni -g omni /var/lib/omni

WORKDIR /app

# Copy only the venv and the source/migrations the app needs to run.
COPY --from=builder --chown=omni:omni /app/.venv      /app/.venv
COPY --from=builder --chown=omni:omni /app/src        /app/src
COPY --from=builder --chown=omni:omni /app/migrations /app/migrations

USER omni

EXPOSE 8000

# /health is provided by Neutron; it only answers once the lifespan (which runs
# migrations) has completed, so a healthy container == migrated and serving.
# python:3.12-slim ships no curl, so use the stdlib for the probe.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()" || exit 1

CMD ["uvicorn", "omni.main:app", "--host", "0.0.0.0", "--port", "8000"]
