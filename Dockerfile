# Kroger MCP server over Streamable HTTP, at /mcp on port 8000.
#
# The server has no client authentication. Run it on a private network, behind
# something that authenticates its clients.
#
#   docker build -t kroger-mcp .
#   docker run --rm -p 127.0.0.1:8000:8000 -v kroger-data:/data \
#       -e KROGER_CLIENT_ID -e KROGER_CLIENT_SECRET kroger-mcp

FROM python:3.12-slim AS build

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.13.7

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# The locked versions, without the dev tools, installed into /app/.venv
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim

RUN useradd --system --uid 10001 --create-home --home-dir /home/kroger kroger \
    && mkdir /data && chown kroger /data

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    KROGER_TOKEN_DIR=/data

# Saved sign-in, preferred store, and the local cart record
VOLUME /data
USER kroger
WORKDIR /home/kroger
EXPOSE 8000

# Liveness only: the route never calls Kroger and stays 200 when the sign-in needs attention
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["kroger-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
