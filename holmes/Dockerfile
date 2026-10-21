# ============================================================================
# HolmesGPT (CLI) — AI điều tra sự cố, dùng model trên AWS Bedrock.
#
# Tự build từ PyPI (ghim phiên bản) thay vì dùng image dựng sẵn của nhà phát
# triển: image đó nằm trong registry "devel", không có tag phiên bản cố định.
# holmesgpt đã kèm boto3 + litellm (thư viện gọi Bedrock), không cần cài thêm.
# ============================================================================
FROM python:3.12-slim

ARG HOLMESGPT_VERSION=0.45.0

RUN pip install --no-cache-dir "holmesgpt==${HOLMESGPT_VERSION}" \
    # Chạy bằng user thường, không phải root
    && useradd --create-home --uid 10001 holmes \
    # Thư mục cache/lịch sử của CLI phải ghi được
    && mkdir -p /home/holmes/.holmes && chown holmes:holmes /home/holmes/.holmes

COPY --chmod=755 entrypoint.sh /usr/local/bin/holmes-entrypoint.sh
# Đề phòng file bị lưu kiểu xuống dòng Windows (CRLF) -> sh không chạy được
RUN sed -i 's/\r$//' /usr/local/bin/holmes-entrypoint.sh

USER holmes
WORKDIR /home/holmes

# Cấu hình toolset (Prometheus / Loki / Alertmanager) được mount vào
# ~/.holmes/config.yaml, đường dẫn mặc định của CLI (xem docker-compose.yml).
ENTRYPOINT ["/usr/local/bin/holmes-entrypoint.sh"]
CMD ["--help"]
