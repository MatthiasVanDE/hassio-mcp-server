# Built by CI and published to GHCR for both architectures; the Supervisor pulls it
# rather than building anything, which is the difference between an install that takes
# seconds and one that compiles for minutes on a Raspberry Pi. It still builds by hand
# -- `docker build ./ha_mcp_server` -- which is how you check what is in it.
#
# A plain `FROM`, not BUILD_FROM with a build.yaml: that legacy builder was retired
# in Supervisor 2026.04. Because the upstream Python image is published as a
# multi-architecture manifest, one `FROM` covers both aarch64 and amd64 without a
# build matrix. Debian slim rather than Alpine, so pip installs a normal manylinux
# wheel instead of compiling one on a Raspberry Pi.
FROM python:3.12-slim

# websockets is the only dependency: the REST API is reached with urllib from the
# standard library, but Home Assistant's WebSocket API is not. That API is exactly
# what is needed for everything the web interface can do and REST cannot offer:
# registries, integrations, dashboards, backups.
#
# tzdata gives zoneinfo its database, so the time zone Home Assistant reports can
# actually be resolved; the slim image ships without it.
#
# Both are pinned, so the image published under a version tag is the same code for
# everyone who installs it -- including anyone who builds it themselves months later.
# Dependabot raises these.
RUN pip install --no-cache-dir \
      websockets==17.1 \
      tzdata==2026.4

COPY server.py /server.py

ENV PYTHONUNBUFFERED=1

# Labels the Supervisor and the add-on store read back.
# Overridden by CI with the version from config.yaml; the default keeps a hand-built
# image honest about what it is.
ARG BUILD_VERSION="dev"
LABEL \
  io.hass.name="MCP Server (Home Assistant API)" \
  io.hass.description="Expose the full Home Assistant REST, WebSocket and Supervisor API as MCP tools" \
  io.hass.type="addon" \
  io.hass.version="${BUILD_VERSION}" \
  org.opencontainers.image.source="https://github.com/MatthiasVanDE/hassio-mcp-server" \
  org.opencontainers.image.licenses="MIT"

EXPOSE 8099

# This is what restarts the add-on when it stops answering, replacing the obsolete
# add-on `watchdog` option. That is the difference between "it runs" and "it is in
# production".
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=4).status == 200 else 1)"

CMD ["python3", "/server.py"]
