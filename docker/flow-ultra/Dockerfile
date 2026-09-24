# flow-ultra — an always-on Chromium on a server (cf-worker-01) holding one Flow account, with the Flow2API
# worker extension. The extension is NOT baked in: /opt/ext is a read-only bind mount of /srv/flow-ultra/ext,
# which ext-sync.sh keeps equal to the package published in the Flow2API admin (+ this box's site.json).
# Build: docker build -t flow-browser:<n> docker/flow-ultra
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99 LANG=C.UTF-8 TZ=UTC
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl tini xvfb openbox x11vnc xdotool imagemagick x11-utils procps \
      fonts-liberation fonts-noto-color-emoji fonts-dejavu-core dbus-x11 chromium \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh && useradd -m -u 1000 -s /bin/bash fu \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix && mkdir -p /profile /opt/ext && chown -R fu:fu /profile
VOLUME ["/profile"]
USER fu
WORKDIR /home/fu
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/entrypoint.sh"]
