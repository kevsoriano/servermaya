FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY main.py extraction.py ./
CMD ["sh", "-c", "exec .venv/bin/uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
