# Claude Replay — container image for the MCP server / dashboard.
#
# Two stages so the final image carries no build toolchain: stage 1 builds a
# wheel, stage 2 installs just that wheel into a slim runtime.
#
#   docker build -t claude-replay .
#   # HTTP dashboard + JSON API on :8766 (default):
#   docker run -p 8766:8766 -v claude-replay-data:/data claude-replay
#   # MCP tools over stdio (for MCP clients / directory build tests):
#   docker run -i --rm -v claude-replay-data:/data claude-replay mcp
#
# Note: session *recording* happens via Claude Code hooks on the host, which
# write to ~/.claude-replay/sessions.db. To serve real recordings from the
# container, mount that store at /data (CLAUDE_REPLAY_DB=/data/sessions.db);
# otherwise the MCP tools run against an empty store.

FROM python:3.12-slim AS build
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist

FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.source="https://github.com/constripacity/Claude-Replay" \
      org.opencontainers.image.description="Session checkpoint & recovery layer for Claude Code." \
      org.opencontainers.image.licenses="MIT"

# Run unprivileged. /data holds the SQLite session store and is the documented volume.
RUN useradd --create-home --uid 10001 replay \
    && mkdir -p /data \
    && chown replay:replay /data
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl

USER replay
WORKDIR /home/replay
ENV CLAUDE_REPLAY_DB=/data/sessions.db
VOLUME ["/data"]
EXPOSE 8766

# Healthcheck hits the unauthenticated /status endpoint (HTTP mode only).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/status', timeout=2).status==200 else 1)"

# Default to the HTTP dashboard/API; run `... mcp` for the stdio MCP transport.
ENTRYPOINT ["claude-replay"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8766"]
