#!/bin/sh
# Sinh file htpasswd LÚC CONTAINER CHẠY (không bake vào image, vì image này
# public trên Docker Hub) — dùng cho Basic Auth bảo vệ /admin/grafana/,
# /admin/prometheus/, /admin/alertmanager/ (xem app-locations.conf), TÁCH
# RIÊNG khỏi hệ thống login admin của shop (vốn phụ thuộc MySQL qua
# auth_request -> nếu MySQL chết thì monitoring cũng mù theo, xem README).
#
# Định dạng {SSHA}: nginx auth_basic hỗ trợ sẵn, không phụ thuộc crypt() của
# libc -> an toàn trên Alpine/musl (mặc định htpasswd hay tạo $apr1$ (MD5),
# musl không hỗ trợ tốt dạng đó).
set -eu
: "${MONITORING_BASIC_AUTH_USER:?set MONITORING_BASIC_AUTH_USER in .env.app}"
: "${MONITORING_BASIC_AUTH_PASSWORD:?set MONITORING_BASIC_AUTH_PASSWORD in .env.app}"

htpasswd -nbs "$MONITORING_BASIC_AUTH_USER" "$MONITORING_BASIC_AUTH_PASSWORD" > /etc/nginx/monitoring.htpasswd

exec nginx -g 'daemon off;'
