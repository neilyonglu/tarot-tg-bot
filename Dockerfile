FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY app.py ./
COPY bot/ ./bot/
COPY assets/ ./assets/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
RUN python -m tests.test_app

USER 10001:10001
CMD ["python", "app.py"]
