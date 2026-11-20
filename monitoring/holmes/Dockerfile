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

# Đóng gói config.yaml vào image (không bind-mount từ host nữa — dùng cho
# chế độ "1 EC2, 2 docker-compose" + Docker Hub, xem README). Không có secret
# trong file này, chỉ có URL nội bộ (prometheus/loki/alertmanager).
COPY --chown=holmes:holmes config.yaml /home/holmes/.holmes/config.yaml

USER holmes
WORKDIR /home/holmes

# Cấu hình toolset (Prometheus / Loki / Alertmanager) nằm ở ~/.holmes/config.yaml,
# đường dẫn mặc định của CLI (đã COPY vào image ở trên).
ENTRYPOINT ["/usr/local/bin/holmes-entrypoint.sh"]
CMD ["--help"]
