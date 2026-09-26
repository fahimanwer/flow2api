# flow-ultra — an always-on Chromium on a server (cf-worker-01) holding one Flow account, with the Flow2API
# worker extension. The extension is NOT baked in: /opt/ext is a read-only bind mount of /srv/flow-ultra/ext,
# which ext-sync.sh keeps equal to the package published in the Flow2API admin (+ this box's site.json).
# Build: docker build -t flow-browser:<n> docker/flow-ultra
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive DISPLAY=:99 LANG=C.UTF-8 TZ=UTC
# Real Google Chrome, not Chromium: a fresh account signed in on Chromium here was signed out by Google within
# ~40 s (2026-09-26); the pinterest fleet image uses branded Chrome the same way (amd64 only).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg tini xvfb openbox x11vnc xdotool imagemagick x11-utils procps python3 \
      fonts-liberation fonts-noto-color-emoji fonts-dejavu-core dbus-x11 \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /opt/entrypoint.sh
COPY register-ext.py /opt/register-ext.py
COPY proxy-forward.py /opt/proxy-forward.py
RUN chmod +x /opt/entrypoint.sh && useradd -m -u 1000 -s /bin/bash fu \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix && mkdir -p /profile && chown -R fu:fu /profile /opt
VOLUME ["/profile"]
USER fu
WORKDIR /home/fu
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/entrypoint.sh"]
