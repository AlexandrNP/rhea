FROM docker:24-dind AS docker-cli

FROM ghcr.io/astral-sh/uv:python3.10-bookworm-slim

WORKDIR /app

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
# requested per-tool by rhea at env-build time. Installed BEFORE the source
# COPY so a source edit does not force a multi-minute conda re-download.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ARCH="$(uname -m)" \
    && curl -fsSL "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${ARCH}.sh" -o /tmp/miniforge.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm -f /tmp/miniforge.sh \
    && /opt/conda/bin/conda clean -afy

# conda on PATH + the canonical $CONDA_EXE the rhea agent resolves
# (rhea/agent/utils.py::_conda_binary).
#
# RHEA_CONDA_ENVS_DIR is the unpack target for a CACHED per-tool conda env
# (rhea/agent/utils.py::install_conda_env -> unpack_conda_env). It MUST be
# conda's DEFAULT named-env dir (/opt/conda/envs), because the tool RUN step
# (rhea/agent/tool.py: `conda run -n <tool.id>`) resolves the env BY NAME via
# conda's envs_dirs. A separate dir (the old /opt/rhea-conda/envs) only worked
# on a COLD build in the same container — `conda create -n <tool>` writes to
# /opt/conda/envs, which `conda run -n` then finds — but on a FRESH container
# with a WARM Redis conda_envs cache (the orchestrator's 2nd-start shape) the
# env unpacks to RHEA_CONDA_ENVS_DIR while `conda run -n` looks in /opt/conda/envs
# -> EnvironmentLocationNotFound. Unifying the two paths fixes the warm-cache run.
ENV PATH=/opt/conda/bin:$PATH
ENV CONDA_EXE=/opt/conda/bin/conda
ENV RHEA_CONDA_ENVS_DIR=/opt/conda/envs

COPY . /app/

# Raise uv's HTTP timeout from the 30s default: `uv sync` downloads the full
# Parsl/Academy/proxystore dependency closure, and on a slow/contended network
# a single package (trio, psutil, …) routinely exceeds 30s and fails the build.
# 120s makes the build robust to slow networks without masking a real hang.
ENV UV_HTTP_TIMEOUT=120
RUN uv sync --locked

CMD ["uv", "run", "-m", "rhea.server.mcp_server", "--transport", "streamable-http"]
