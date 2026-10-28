# Serve the static frontend with nginx
FROM nginx:1.27-alpine
# Shop: index.html + styles.css + app.js
# Đăng nhập: login.html + login.js (công khai)
# Admin: admin.html + admin.css + admin.js, tools.js = mục Monitoring (link Grafana, Prometheus...)
#   -> nginx chỉ trả các file admin cho admin đã đăng nhập (nginx/snippets/app-locations.conf)
COPY index.html styles.css app.js login.html login.js admin.html admin.css admin.js tools.js /usr/share/nginx/html/
# Logo các tool (lấy từ chính Grafana/Prometheus/Alloy/mailpit)
COPY icons /usr/share/nginx/html/icons/
# Template Tabler (CSS) cho trang admin, lưu sẵn để không phụ thuộc CDN
COPY vendor /usr/share/nginx/html/vendor/
EXPOSE 80
