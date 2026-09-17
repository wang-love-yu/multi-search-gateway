FROM node:22-alpine AS ui
WORKDIR /ui
ENV NEXT_TELEMETRY_DISABLED=1
COPY frontend/package*.json ./
# A lockfile, when present, is authoritative. An offline scaffold can still be bootstrapped.
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY frontend/ ./
RUN npm run typecheck && npm run build
COPY scripts/build-csp.mjs /build-csp.mjs
RUN node /build-csp.mjs /ui/out /ui/csp.caddy

FROM caddy:2.10.2-alpine
RUN mkdir -p /srv/admin /data /config && chown -R 10001:10001 /data /config /srv/admin
COPY --from=ui /ui/out/ /srv/admin/
COPY --from=ui /ui/csp.caddy /etc/caddy/csp.caddy
COPY deploy/Caddyfile /etc/caddy/Caddyfile
USER 10001:10001
EXPOSE 8080 8443
