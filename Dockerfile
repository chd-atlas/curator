# curator: chd-curator, its dependencies, and the atlas command line's dependencies.
#
# The atlas CODE is never baked in. It always runs from the atlas checkout a pipeline mounts, with
# CHD_ATLAS_SRC=<checkout>/src (see docker/chd-atlas), so the code and the data it reads match.
# Only the atlas's dependencies are installed, resolved from its uv.lock at ATLAS_COMMIT.
#
#   docker build --build-arg ATLAS_COMMIT=<40-hex commit of chd-atlas.github.io> -t curator .
#
# Layout: /opt/curator-venv (chd-curator, on PATH), /opt/atlas-venv (the atlas's dependencies).

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64

ARG ATLAS_COMMIT
ARG ATLAS_REPO=chd-atlas/chd-atlas.github.io

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
RUN python -c 'import os, urllib.request; base = "https://raw.githubusercontent.com/" + os.environ["ATLAS_REPO"] + "/" + os.environ["ATLAS_COMMIT"] + "/"; [open(n, "wb").write(urllib.request.urlopen(base + n, timeout=60).read()) for n in ("pyproject.toml", "uv.lock")]'
RUN UV_PROJECT_ENVIRONMENT=/opt/atlas-venv uv sync --locked --no-dev --no-install-project

COPY docker/chd-atlas /usr/local/bin/chd-atlas
RUN chmod 0755 /usr/local/bin/chd-atlas

ENV PATH=/opt/curator-venv/bin:$PATH \
    CHD_ATLAS_CLI=/usr/local/bin/chd-atlas \
    CHD_ATLAS_PYTHON=/opt/atlas-venv/bin/python

WORKDIR /work
ENTRYPOINT ["chd-curator"]
