# Nginx với config đã đóng gói sẵn — dùng cho chế độ triển khai Docker Hub +
# EC2 user-data (xem README mục 12). KHÔNG cần bind-mount config.conf từ host
# nữa: EC2 chỉ cần pull image này, không cần copy file nginx/* nào lên máy.
#
# entrypoint.sh sinh file htpasswd LÚC CHẠY (không bake vào image public) để
# Basic Auth bảo vệ 3 route monitoring, tách riêng khỏi login admin của shop.
# apache2-utils cung cấp lệnh htpasswd (dùng cờ -s tạo hash {SSHA}, hợp musl).
FROM nginx:1.27-alpine
RUN apk add --no-cache apache2-utils
COPY default.conf /etc/nginx/conf.d/default.conf
COPY snippets /etc/nginx/snippets
COPY entrypoint.sh /entrypoint.sh
# acme.conf trỏ tới /var/www/certbot — tạo sẵn thư mục rỗng để nginx không lỗi
# khi start (HTTPS/Let's Encrypt đang để sau, chưa dùng tới trong chế độ này).
RUN chmod +x /entrypoint.sh && mkdir -p /var/www/certbot
EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]
