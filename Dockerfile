# syntax=docker/dockerfile:1
#
# Omni Analyst v2 - API image.
#
# Stateless uvicorn process serving JSON. Migrations run in the app lifespan on
# startup, so this image is self-contained: bring it up pointed at a reachable
# Postgres and it will migrate then serve.
#
# About neutron-py: the app depends on Neutron as an *editable local path*
# (pyproject.toml [tool.uv.sources] -> ../../Neutron/python). That path lives
# outside any build context rooted at the repo, and Docker cannot COPY above the
# context. The operator therefore builds the Neutron wheel on the host and
# places it under vendor/ before building. See DEPLOY.md ("Build prerequisites")
# for the one-line command. The wheel is pinned by filename; pass
# --build-arg NEUTRON_WHEEL=... if the Neutron version differs.

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

# Stand up the project venv first; everything installs into it.
RUN uv venv /app/.venv

# Install the project's third-party dependencies from the lock, WITHOUT the
# project itself and WITHOUT the editable neutron path. We export the frozen
# lock to a requirements stream, strip the editable (-e ../../Neutron/python)
# line and any neutron-py reference, and install the rest verbatim. This keeps
# the build reproducible against uv.lock for every registry dependency.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-annotate \
        | grep -vE '^[[:space:]]*-e[[:space:]]' \
        | grep -vE 'neutron-py' \
        > /tmp/requirements.txt \
 && uv pip install --python /app/.venv/bin/python -r /tmp/requirements.txt

# Now the local framework, from the operator-built wheel. Placed after the heavy
# registry install so a Neutron rebuild invalidates only this thin layer.
ARG NEUTRON_WHEEL=vendor/neutron_py-0.1.0-py3-none-any.whl
# Copy into a directory rather than to a fixed filename. A wheel renamed to
# neutron.whl loses its version, and uv rejects it: PEP 427 filenames carry the
# version and installers parse it rather than reading metadata first.
COPY ${NEUTRON_WHEEL} /tmp/wheels/
RUN uv pip install --python /app/.venv/bin/python /tmp/wheels/*.whl

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

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:${PATH}

# Non-root user. uid 10001 avoids colliding with any host uid bind-mounted in.
RUN useradd --create-home --uid 10001 --shell /sbin/nologin omni

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
