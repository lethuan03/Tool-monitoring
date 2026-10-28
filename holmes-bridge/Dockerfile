# ============================================================================
# holmes-bridge — cầu nối Alertmanager -> HolmesGPT (Bedrock) -> Telegram.
#
# Dựng TRÊN image ecommerce-holmes (đã có holmesgpt, config.yaml nối
# Prometheus/Loki/Alertmanager, script làm sạch khoá AWS), chỉ thêm 1 file
# Python dùng thư viện chuẩn. Không có secret nào trong image: token Telegram
# và khoá Bedrock chỉ đọc từ .env lúc container chạy.
# ============================================================================
ARG HOLMES_IMAGE=thuanperry/ecommerce-holmes:0.45.0
FROM ${HOLMES_IMAGE}

COPY bridge.py /app/bridge.py

# Chỉ nghe trong mạng Docker nội bộ (không publish port ra máy chủ)
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"

# Image gốc có ENTRYPOINT = holmes CLI và CMD ["--help"] -> ghi đè cả hai
ENTRYPOINT ["python", "-u", "/app/bridge.py"]
CMD []
