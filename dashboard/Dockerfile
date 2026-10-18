# The dashboard: built once with Node, then served as static files by nginx, which can
# also forward /admin and /gateway to the gateway (the same job Vite's dev server does).
FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY . .
RUN npm run build

FROM nginx:1.29-alpine
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --chmod=755 nginx/40-gateway-proxy.sh /docker-entrypoint.d/
# Where /admin and /gateway are forwarded (the Compose service). Empty = don't proxy.
ENV GATEWAY_UPSTREAM=http://gateway:8000
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
