# --- pelican CLI ------------------------------------------------------------
# Static Go binary from the upstream release; bump PELICAN_VERSION to upgrade
# (docker-compose.api.yml sets it as a build arg). checksums.txt comes from the
# same release, so the download is integrity-checked without pinning a
# per-version hash here.
FROM debian:12-slim AS pelican
ARG PELICAN_VERSION=7.25.0
ARG PELICAN_ARCH=x86_64
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /out
RUN set -eux; \
    base="https://github.com/PelicanPlatform/pelican/releases/download/v${PELICAN_VERSION}"; \
    asset="pelican_Linux_${PELICAN_ARCH}.tar.gz"; \
    curl -fsSL -O "$base/$asset"; \
    curl -fsSL -O "$base/checksums.txt"; \
    sha256sum --ignore-missing -c checksums.txt; \
    tar -xzf "$asset" --strip-components=1 "pelican-${PELICAN_VERSION}/pelican"; \
    rm -f "$asset" checksums.txt; \
    ./pelican --version

# --- MMseqs2 ----------------------------------------------------------------
# Static build from the upstream GitHub release, for the /v2/query similarity
# search (app/services/similarity). The release ships NO checksum file, so the
# asset's sha256 is pinned here (MMSEQS_SHA256); when bumping MMSEQS_VERSION,
# download the new asset, `sha256sum` it, and update both ARGs together. An
# empty MMSEQS_SHA256 skips verification (dev only). `mmseqs version` prints
# the build's git commit, not the release tag.
FROM debian:12-slim AS mmseqs
ARG MMSEQS_VERSION=18-8cc5c
ARG MMSEQS_ARCH=avx2
ARG MMSEQS_SHA256=bd9b0234da5949ad528d5b5f9ea4cda9c1e23dce14b46c0791d4d919a76e61ce
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /out
RUN set -eux; \
    asset="mmseqs-linux-${MMSEQS_ARCH}.tar.gz"; \
    curl -fsSL -O "https://github.com/soedinglab/MMseqs2/releases/download/${MMSEQS_VERSION}/${asset}"; \
    if [ -n "${MMSEQS_SHA256}" ]; then echo "${MMSEQS_SHA256}  ${asset}" | sha256sum -c -; fi; \
    tar -xzf "$asset" --strip-components=2 mmseqs/bin/mmseqs; \
    rm -f "$asset"; \
    ./mmseqs version

FROM python:3.12-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# openssh-client: POST /v1/admin/classification/jobs/{submit,poll} scp/ssh to
# the NR-BLAST box (app/services/reclass_remote.py). The deploy key and the
# pinned known_hosts are mounted read-only at runtime, never baked in.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*

# pelican CLI: `pelican token create` (TOKEN_ISSUER_IMPL=pelican) and
# `pelican object get|rm` (OSDF_CLIENT_IMPL=pelican) run from inside the
# container. Credentials stay OUT of the image — mounted at runtime.
COPY --from=pelican /out/pelican /usr/local/bin/pelican

# mmseqs: similarity search target build + search (SIMILARITY_MMSEQS_BINARY).
COPY --from=mmseqs /out/mmseqs /usr/local/bin/mmseqs

WORKDIR /app

# Dependency layer (cached unless lock/pyproject change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
