# --- Builder ---
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# --- Runtime ---
FROM node:20-alpine
WORKDIR /app
ARG BRAIN_BUILD_SHA=unknown
ARG BRAIN_REQUIRE_RELEASE_IDENTITY=false
RUN if [ "$BRAIN_REQUIRE_RELEASE_IDENTITY" = "true" ]; then \
      echo "$BRAIN_BUILD_SHA" | grep -Eq '^[0-9a-f]{40}$'; \
    fi
LABEL org.opencontainers.image.revision=${BRAIN_BUILD_SHA}
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3100 WEB_HOST=0.0.0.0
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/scripts/serve.mjs ./scripts/serve.mjs
EXPOSE 3100
CMD ["npm", "run", "start"]
