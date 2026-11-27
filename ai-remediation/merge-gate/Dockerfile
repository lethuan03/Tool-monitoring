# merge-gate: hỏi GitHub, gọi Bedrock, nhắn Telegram, tạo PR/merge (xem README2.md mục 11).
# Build context = ai-remediation/ (dùng lại ci-logs/fetch_logs.py). Không có secret trong image.
FROM python:3.12-slim

# boto3 >= 1.39 mới hiểu AWS_BEARER_TOKEN_BEDROCK
RUN pip install --no-cache-dir "boto3>=1.39,<2" \
    && useradd --create-home --uid 10003 gate \
    && mkdir -p /state && chown gate:gate /state

COPY ci-logs/fetch_logs.py merge-gate/gate.py merge-gate/ai.py merge-gate/runner.py merge-gate/rules.md /app/

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STATE_FILE=/state/state.json
USER gate
WORKDIR /app
ENTRYPOINT ["python", "-u", "/app/gate.py"]
