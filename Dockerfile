FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

# Dependencies come from uv.lock (--frozen), so every build installs the versions CI tested.
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY packages ./packages
COPY services ./services
COPY contracts ./contracts
COPY db ./db
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN uv sync --frozen --no-dev --no-editable --no-cache --python /usr/local/bin/python3 \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && useradd --system --uid 10001 --no-create-home empire
USER empire
# Secrets (database URLs, public key PEM) arrive as environment variables at runtime only.
ENTRYPOINT ["docker-entrypoint.sh"]
