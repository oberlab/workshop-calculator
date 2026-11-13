FROM python:3.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# pg_dump for the scheduler's nightly backup (WM-103); must match the PostgreSQL 17 server.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/usr/lib/postgresql/17/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app

# Release tag the image was built from (ops/update.sh passes it); /healthz reports it.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

EXPOSE 8000
# Forwarded headers (scheme, client address) are only trusted from FORWARDED_ALLOW_IPS,
# the reverse proxy's address; the default trusts nobody but the container itself.
# The log config leaves client addresses and query strings out of the access log.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn metrics.web.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\" --log-config /app/src/metrics/web/uvicorn-log.json"]
