#!/bin/sh
# Kiểm tra biến AWS/Bedrock từ .env rồi chạy HolmesGPT CLI.
# KHÔNG in giá trị khoá ra màn hình, chỉ in khoá nào đang có.
set -eu

# docker-compose truyền biến trống ("") khi .env để trống. Biến trống mà vẫn
# tồn tại có thể làm boto3 / litellm chọn nhầm cách đăng nhập -> bỏ hẳn đi.
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_BEARER_TOKEN_BEDROCK AWS_PROFILE; do
  eval "val=\${$v:-}"
  # Bỏ dấu cách, dấu ngoặc kép, \r lỡ dán thừa (lỗi hay gặp khi copy trên Windows)
  val=$(printf '%s' "$val" | tr -d ' \r\n"'"'")
  if [ -z "$val" ]; then unset "$v"; else export "$v=$val"; fi
done

if [ -z "${AWS_REGION_NAME:-}" ]; then
  echo "LỖI: chưa đặt BEDROCK_REGION trong .env (ví dụ us-east-1)." >&2
  exit 1
fi

if [ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ]; then
  echo "Bedrock: dùng API key (AWS_BEARER_TOKEN_BEDROCK), region ${AWS_REGION_NAME}, model ${MODEL}" >&2
elif [ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  extra=""
  [ -n "${AWS_SESSION_TOKEN:-}" ] && extra=" + session token (khoá tạm)"
  echo "Bedrock: dùng access key IAM${extra}, region ${AWS_REGION_NAME}, model ${MODEL}" >&2
else
  echo "LỖI: chưa có thông tin đăng nhập Bedrock trong .env. Đặt MỘT trong hai:" >&2
  echo "  - AWS_BEARER_TOKEN_BEDROCK (API key tạo trên Bedrock console), hoặc" >&2
  echo "  - AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (+ AWS_SESSION_TOKEN nếu là khoá tạm)" >&2
  exit 1
fi

exec holmes "$@"
