# syntax=docker/dockerfile:1

# ── Stage 1: install production dependencies ─────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /build

COPY package*.json ./
RUN npm ci --omit=dev

# ── Stage 2: lean runtime image ───────────────────────────────────────────────
FROM node:20-alpine AS final

# Named non-root user required by policy
RUN addgroup -S appuser && adduser -S -G appuser appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /build/node_modules ./node_modules
COPY --chown=appuser:appuser server.js .
COPY --chown=appuser:appuser public ./public

USER appuser

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD node -e \
    "require('http').get('http://localhost:3000/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
