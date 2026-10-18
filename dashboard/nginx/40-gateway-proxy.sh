#!/bin/sh
# Runs at container start (the nginx image runs /docker-entrypoint.d/*.sh). Writes the
# /admin and /gateway proxy only when GATEWAY_UPSTREAM is set: nginx refuses to start if
# a proxied host can't be resolved, and on ECS there is no "gateway" host to resolve.
set -eu
mkdir -p /etc/nginx/snippets
rm -f /etc/nginx/snippets/gateway-proxy.conf
if [ -n "${GATEWAY_UPSTREAM:-}" ]; then
    cat > /etc/nginx/snippets/gateway-proxy.conf <<CONF
location ~ ^/(admin|gateway)/ {
    proxy_pass ${GATEWAY_UPSTREAM};
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_buffering off;
    # Generating a handoff can take a few minutes (the default is 60s).
    proxy_read_timeout 600s;
}
CONF
    echo "gateway-proxy: /admin and /gateway -> ${GATEWAY_UPSTREAM}"
else
    echo "gateway-proxy: off (GATEWAY_UPSTREAM is empty; serving files only)"
fi
