# Prometheus với config + rules đóng gói sẵn — dùng cho chế độ "1 EC2, 2
# docker-compose (app + monitoring)" nối qua 1 network Docker dùng chung
# (xem README). Target ghi thẳng tên container (ecommerce-backend,
# ecommerce-nginx-exporter...) vì cả 2 compose cùng join network "shared".
FROM prom/prometheus:v3.1.0
COPY prometheus.yml /etc/prometheus/prometheus.yml
COPY rules /etc/prometheus/rules
# Image gốc đã có ENTRYPOINT ["/bin/prometheus"] -> phải xoá (ENTRYPOINT [])
# trước, nếu không CMD bị nối SAU entrypoint cũ thành "/bin/prometheus sh -c ..."
# (lỗi "unexpected sh"). Dùng shell form để biến môi trường
# (PROMETHEUS_RETENTION_TIME/_SIZE) được thay thế lúc container start.
ENTRYPOINT []
CMD ["sh", "-c", "exec /bin/prometheus --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus --storage.tsdb.retention.time=${PROMETHEUS_RETENTION_TIME:-15d} --storage.tsdb.retention.size=${PROMETHEUS_RETENTION_SIZE:-5GB} --web.external-url=${PROMETHEUS_EXTERNAL_URL:-http://localhost/admin/prometheus/} --web.route-prefix=/"]
