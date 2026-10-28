# Alertmanager với khuôn + entrypoint đóng gói sẵn — dùng cho chế độ "1 EC2,
# 2 docker-compose" + Docker Hub (xem README). Secret (TELEGRAM_BOT_TOKEN,
# SMTP_PASSWORD...) vẫn chỉ đọc từ .env lúc container chạy (entrypoint.sh),
# KHÔNG nằm trong image.
FROM prom/alertmanager:v0.34.1
COPY alertmanager.yml /etc/alertmanager/alertmanager.yml
COPY entrypoint.sh /etc/alertmanager/entrypoint.sh
COPY templates /etc/alertmanager/templates
ENTRYPOINT ["/bin/sh", "/etc/alertmanager/entrypoint.sh"]
