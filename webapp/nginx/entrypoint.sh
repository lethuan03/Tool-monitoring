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

# FORCE_HTTPS=true: trình duyệt vào bằng HTTP thì chuyển sang HTTPS (chỉ bật khi ĐÃ có HTTPS ở ALB/certbot,
# nếu không sẽ không vào được web). /health (ALB kiểm tra sức khoẻ bằng HTTP) và ACME (xin cert) không bị chuyển.
case "$(printf '%s' "${FORCE_HTTPS:-false}" | tr 'A-Z' 'a-z')" in
  true|1|yes|on)
    cat > /etc/nginx/snippets/force-https.conf <<'EOF'
set $redirect_https "";
if ($client_proto = "http") { set $redirect_https "1"; }
if ($request_uri = "/health") { set $redirect_https ""; }
if ($request_uri ~ "^/\.well-known/acme-challenge/") { set $redirect_https ""; }
if ($redirect_https) { return 301 https://$host$request_uri; }
EOF
    echo "FORCE_HTTPS: BẬT (HTTP -> HTTPS, trừ /health và ACME)" ;;
  *)
    echo "# FORCE_HTTPS tắt" > /etc/nginx/snippets/force-https.conf
    echo "FORCE_HTTPS: tắt (đang phục vụ HTTP; trên Internet nên bật HTTPS, xem README)" ;;
esac

exec nginx -g 'daemon off;'
