# One image for both central-box services: the controller worker and the dashboard.
# Build context is the repo root (see docker-compose.yml).
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY common.py worker.py starter.py ./
COPY activities ./activities
COPY workflows ./workflows
COPY dashboard ./dashboard

RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app
ENV PATH=/app/.venv/bin:$PATH
