# syntax=docker/dockerfile:1.7
# Multi-stage build for the API server. The final image runs as a non-root user, contains no
# build toolchain, and ships only compiled JS + production dependencies.

# ---- builder ----
FROM node:20.18.1-alpine AS builder
WORKDIR /app

# Install deps with a clean, reproducible lockfile-driven install.
COPY package.json ./
RUN npm install --no-audit --no-fund

COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# Prune to production dependencies for the runtime image.
RUN npm prune --omit=dev

# ---- runtime ----
FROM node:20.18.1-alpine AS runtime
ENV NODE_ENV=production
WORKDIR /app

# Drop a few setuid binaries we never need, then run as the built-in non-root user.
RUN apk add --no-cache tini \
 && rm -f /bin/su /usr/bin/wget 2>/dev/null || true

COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist
COPY package.json ./

USER node
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||8080)+'/v1/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "dist/index.js"]
