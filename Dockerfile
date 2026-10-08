# syntax=docker/dockerfile:1.7

# ---- build: resolve and install into a self-contained venv with uv -----------------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra vault --extra azure

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra vault --extra azure

# ---- runtime ------------------------------------------------------------------------
FROM python:3.12-slim
RUN groupadd --system --gid 10001 m365c \
 && useradd --system --uid 10001 --gid m365c --home-dir /nonexistent --shell /usr/sbin/nologin m365c \
 && mkdir -p /var/lib/m365c && chown m365c:m365c /var/lib/m365c

COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    M365C_CONFIG_DIR=/etc/m365c

USER 10001:10001
WORKDIR /var/lib/m365c
EXPOSE 9365

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9365/healthz', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["m365c"]
CMD ["serve"]
