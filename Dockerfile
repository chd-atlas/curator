# curator: chd-curator, its dependencies, and the atlas command line's dependencies.
#
# The atlas CODE is never baked in. It always runs from the atlas checkout a pipeline mounts, with
# CHD_ATLAS_SRC=<checkout>/src (see docker/chd-atlas), so the code and the data it reads match.
# Only the atlas's dependencies are installed, resolved from its uv.lock at ATLAS_COMMIT.
#
#   docker build --build-arg CURATOR_REVISION=<curator commit> -t curator .
#
# ATLAS_COMMIT (a commit of chd-atlas.github.io) defaults to the pinned one below; it changes only by
# a deliberate edit here and in .github/workflows/image.yml (CI checks the two agree). The commit
# used is recorded in the label org.chd-atlas.atlas-commit.
#
# Layout: /opt/curator-venv (chd-curator, on PATH), /opt/atlas-venv (the atlas's dependencies).

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64

ARG ATLAS_COMMIT=36a06acac765fda1d28cdd893649dacec6f02fe8
ARG CURATOR_REVISION=unknown
ARG ATLAS_REPO=chd-atlas/chd-atlas.github.io

LABEL org.opencontainers.image.source="https://github.com/chd-atlas/curator" \
      org.opencontainers.image.revision="${CURATOR_REVISION}" \
      org.chd-atlas.atlas-commit="${ATLAS_COMMIT}"

# procps: Nextflow's task wrapper calls `ps` to collect task metrics.
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

# Use the image's Python; never let uv download another.
ENV UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# chd-curator and its locked runtime dependencies (no dev group).
WORKDIR /opt/curator
COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/opt/curator-venv uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN UV_PROJECT_ENVIRONMENT=/opt/curator-venv uv sync --locked --no-dev --no-editable

# The atlas's dependencies only: pyproject.toml and uv.lock at ATLAS_COMMIT, no atlas code.
WORKDIR /opt/atlas-deps
RUN test -n "${ATLAS_COMMIT}" || { echo "build-arg ATLAS_COMMIT is required" >&2; exit 2; }
COPY docker/fetch_atlas_lock.py /opt/fetch_atlas_lock.py
RUN python /opt/fetch_atlas_lock.py "${ATLAS_REPO}" "${ATLAS_COMMIT}"
RUN UV_PROJECT_ENVIRONMENT=/opt/atlas-venv uv sync --locked --no-dev --no-install-project

COPY docker/chd-atlas /usr/local/bin/chd-atlas
RUN chmod 0755 /usr/local/bin/chd-atlas

ENV PATH=/opt/curator-venv/bin:$PATH \
    CHD_ATLAS_CLI=/usr/local/bin/chd-atlas

WORKDIR /work
# No ENTRYPOINT: a pipeline runs each task as a command (a bash wrapper), which an ENTRYPOINT would
# turn into arguments to chd-curator.
CMD ["chd-curator", "--help"]
