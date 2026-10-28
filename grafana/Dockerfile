# Grafana với provisioning + dashboard đóng gói sẵn — dùng cho chế độ "1 EC2,
# 2 docker-compose" + Docker Hub (xem README). Không bind-mount gì từ host.
FROM grafana/grafana:11.3.0
COPY provisioning /etc/grafana/provisioning
COPY dashboards /etc/grafana/dashboards
