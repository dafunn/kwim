# KWIM admin console - the browser SPA over /v1/admin.
# Self-contained: build context is this directory.
#   docker build -f services/console/Dockerfile -t <tag> services/console
#
# src/api/schema.d.ts is committed; scripts/generate-api-types.sh regenerates it.
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN npm ci
COPY . .
RUN npm run build

# Runtime stage carries no node runtime and no build tooling - just the
# built static assets and a web server.
FROM nginx:stable-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
