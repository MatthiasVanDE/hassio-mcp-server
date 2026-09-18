# The Supervisor builds this image on the Home Assistant machine itself.
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
# Both are pinned. This image is rebuilt on each user's own machine, at whatever date
# they happen to install it, so an unpinned version means no two installations are
# necessarily running the same code. Dependabot raises these.
RUN pip install --no-cache-dir \
      websockets==17.1 \
      tzdata==2026.4

COPY server.py /server.py

ENV PYTHONUNBUFFERED=1

# Labels the Supervisor and the add-on store read back.
ARG BUILD_VERSION="2.0.0"
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
