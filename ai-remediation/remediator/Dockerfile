# ============================================================================
# remediator — thực thi lệnh sửa lỗi ĐÃ ĐƯỢC NGƯỜI DUYỆT (Telegram), theo policy.json.
# Chỉ dùng thư viện chuẩn Python. Không có secret trong image: token Telegram và
# khoá ký chỉ đọc từ biến môi trường lúc chạy.
# Không chạm docker.sock: chỉ gọi Docker API qua service docker-socket-proxy.
# ============================================================================
FROM python:3.12-slim

RUN useradd --create-home --uid 10002 remediator \
    && mkdir -p /audit && chown remediator:remediator /audit

COPY remediator.py policy.json /app/

USER remediator
WORKDIR /app
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=3)"

ENTRYPOINT ["python", "-u", "/app/remediator.py"]
