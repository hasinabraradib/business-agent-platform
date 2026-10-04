# The dashboard as a small Node server (Next.js standalone output).
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# Retries ride out a dropped connection to the registry.
ENV npm_config_fetch_retries=5 npm_config_fetch_retry_mintimeout=10000 npm_config_fetch_retry_maxtimeout=120000
RUN PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci --no-audit --no-fund --loglevel=error

FROM node:22-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3001 HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3001
CMD ["node", "server.js"]
