# Alloy (bản EC2/remote) với config đã đóng gói sẵn — dùng cho chế độ triển
# khai Docker Hub + EC2 user-data (xem README mục 12). KHÔNG cần bind-mount
# config.remote.alloy từ host nữa: EC2 chỉ cần pull image này.
#
# LOKI_PUSH_URL vẫn đọc lúc CHẠY qua sys.env("LOKI_PUSH_URL") trong
# config.remote.alloy (xem file đó) — không bake cứng vào image, đặt trong
# .env.app lúc deploy.
FROM grafana/alloy:v1.20.1
COPY config.remote.alloy /etc/alloy/config.alloy
CMD ["run", "--server.http.listen-addr=0.0.0.0:12345", "--storage.path=/var/lib/alloy/data", "/etc/alloy/config.alloy"]
