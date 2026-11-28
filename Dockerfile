# Server mode of tg-scribe: python -m tgc.server --http behind Caddy (see docker-compose.yml).
FROM python:3.12-slim AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng antiword \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
# The venv sits outside /app, so the bind-mounted archive and a stray local .venv cannot shadow it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH HOME=/home/app PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY tgc ./tgc

# docker build --target test .  — the suite with the Linux tools required, not skipped
FROM base AS test
RUN uv sync --frozen --no-install-project
COPY tests ./tests
RUN TGC_REQUIRE_LINUX_TOOLS=1 python -m pytest -q

FROM base AS app
# Any UID may run it (compose sets TGC_UID): it writes only to the bind-mounted data directories.
RUN mkdir -p /home/app/.tgc /app/archive
EXPOSE 8000
CMD ["python", "-m", "tgc.server", "--http"]
