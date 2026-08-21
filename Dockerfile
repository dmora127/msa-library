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

FROM python:3.12-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# pelican CLI: `pelican token create` (TOKEN_ISSUER_IMPL=pelican) and
# `pelican object get|rm` (OSDF_CLIENT_IMPL=pelican) run from inside the
# container. Credentials stay OUT of the image — mounted at runtime.
COPY --from=pelican /out/pelican /usr/local/bin/pelican

WORKDIR /app

# Dependency layer (cached unless lock/pyproject change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
