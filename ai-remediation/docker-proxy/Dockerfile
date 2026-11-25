# ============================================================================
# docker-proxy (allowlist) — cổng duy nhất tới Docker API cho remediator.
# Thay tecnativa/docker-socket-proxy (cho tạo/xoá container khi bật POST).
# Chỉ thư viện chuẩn Python, không phụ thuộc gì thêm. Xem proxy.py.
# Chạy với root CHỈ để mở được /var/run/docker.sock; compose bỏ hết capability,
# hệ thống file chỉ đọc, no-new-privileges.
# ============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY proxy.py /app/proxy.py

EXPOSE 2375
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:2375/_ping',timeout=3)"

ENTRYPOINT ["python", "-u", "/app/proxy.py"]
