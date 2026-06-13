FROM docker:24-dind AS docker-cli

FROM ghcr.io/astral-sh/uv:python3.10-bookworm-slim

WORKDIR /app

COPY . /app/

# Install "docker" command into the container
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins

ENV PYTHONUNBUFFERED=1

# Bind on all interfaces by default. The streamable-http MCP server reads
# HOST from Settings (rhea/server/schema.py) which pydantic-settings maps
# from the $HOST env var. The Settings DEFAULT is "localhost" so the
# host-process path (uv run locally) stays loopback-only — but a server
# running INSIDE a container that binds the container loopback is
# unreachable from the host even with `-p 3001:3001`. Baking HOST=0.0.0.0
# into the image makes `docker run -p 3001:3001 <image>` reachable from
# the host out of the box, without the caller having to remember the bind
# fix. This is the load-bearing container-loopback fix.
ENV HOST=0.0.0.0

# Bake a conda toolchain for the `local` Parsl backend. Rhea's tool actor
# (rhea/agent/tool.py + rhea/agent/utils.py) builds a per-tool conda env
# (bioconda + conda-forge) to EXECUTE a Galaxy tool. The `local` Parsl
# backend runs that actor as a plain subprocess inside THIS container, so
# `conda` must exist on PATH here. Tool DISCOVERY (find_tools / the
# determinism wire) does NOT need conda; this is baked for tool-execution
# completeness so a fully-deterministic run works inside the container.
# Miniforge ships `conda` + the conda-forge default channel; bioconda is
# requested per-tool by rhea at env-build time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ARCH="$(uname -m)" \
    && curl -fsSL "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${ARCH}.sh" -o /tmp/miniforge.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm -f /tmp/miniforge.sh \
    && /opt/conda/bin/conda clean -afy

# conda on PATH + the canonical $CONDA_EXE the rhea agent resolves
# (rhea/agent/utils.py::_conda_binary). A writable per-tool envs dir that
# is NOT /home/rhea (which does not exist as a writable path in this
# image) — rhea/agent/tool.py reads $RHEA_CONDA_ENVS_DIR.
ENV PATH=/opt/conda/bin:$PATH
ENV CONDA_EXE=/opt/conda/bin/conda
ENV RHEA_CONDA_ENVS_DIR=/opt/rhea-conda/envs
RUN mkdir -p /opt/rhea-conda/envs

RUN uv sync --locked

CMD ["uv", "run", "-m", "rhea.server.mcp_server", "--transport", "streamable-http"]
