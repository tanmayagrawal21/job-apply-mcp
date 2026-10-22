# job-apply-mcp over streamable HTTP (ADR-0020).
#   docker build -t job-apply-mcp .
#   docker build --build-arg BROWSER=1 -t job-apply-mcp:browser .     # adds Playwright + Chromium
#   docker run --rm -p 127.0.0.1:8765:8765 -e JOB_APPLY_MCP_TOKEN="$(openssl rand -base64 32)" \
#     -v jam-config:/home/user/.config/job-apply-mcp -v jam-data:/home/user/.local/share/job-apply-mcp job-apply-mcp
# The server refuses to listen on 0.0.0.0 without JOB_APPLY_MCP_TOKEN. Publish the port on 127.0.0.1 only,
# or put a TLS reverse proxy in front.
FROM python:3.12-slim AS base

ARG BROWSER=0
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY examples ./examples
RUN if [ "$BROWSER" = "1" ]; then pip install ".[browser]" && playwright install --with-deps chromium; \
    else pip install .; fi \
    && rm -rf /src

RUN useradd --create-home --uid 10001 user && mkdir -p /home/user/.config/job-apply-mcp /home/user/.local/share/job-apply-mcp \
    && chown -R user:user /home/user && { [ ! -d /opt/ms-playwright ] || chmod -R a+rX /opt/ms-playwright; }
USER user
WORKDIR /home/user
# Create config on first start: `docker run ... job-apply-mcp init --yes` (mount the config volume).

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2).status == 200 else 1)"
ENTRYPOINT ["job-apply-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
