# wallexer engine image (Liara). Secrets come only from Liara env vars, never from the image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    WALLEXER_IN_CONTAINER=1

RUN pip install --no-cache-dir uv

WORKDIR /app
# Dependencies first (cached layer), then the project.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev \
 && mkdir -p /app/seed \
 && cp data/not_allowed.txt data/unavailable_markets.txt /app/seed/ \
 && chmod +x deploy/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
