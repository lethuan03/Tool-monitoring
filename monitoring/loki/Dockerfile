# Loki với config đóng gói sẵn — dùng cho chế độ "1 EC2, 2 docker-compose" +
# Docker Hub (xem README). LOKI_RETENTION_PERIOD vẫn đọc từ .env lúc chạy
# qua -config.expand-env=true (file dùng ${LOKI_RETENTION_PERIOD}).
FROM grafana/loki:3.7.6
COPY loki-config.yml /etc/loki/loki-config.yml
CMD ["-config.file=/etc/loki/loki-config.yml", "-config.expand-env=true"]
