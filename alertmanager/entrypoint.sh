#!/bin/sh
# Điền giá trị từ .env vào khuôn alertmanager.yml, bật/tắt kênh Telegram và mail,
# rồi chạy Alertmanager.
# Chạy bên trong container (image có sẵn sh + sed). Xem README mục 7.14 (mail) và 7.15 (Telegram).
set -eu

SMTP_HOST="${SMTP_HOST:-}"
SMTP_USERNAME="${SMTP_USERNAME:-}"
SMTP_REQUIRE_TLS="${SMTP_REQUIRE_TLS:-true}"
# Bỏ dấu cách, dấu ngoặc kép và ký tự xuống dòng kiểu Windows (\r) lỡ dán thừa
# vào token / chat ID trong .env: thừa 1 ký tự là Telegram báo Unauthorized.
TELEGRAM_BOT_TOKEN=$(printf '%s' "${TELEGRAM_BOT_TOKEN:-}" | tr -d ' \r\n"'"'")
TELEGRAM_CHAT_ID=$(printf '%s' "${TELEGRAM_CHAT_ID:-}" | tr -d ' \r\n"'"'")
TELEGRAM_API_URL="${TELEGRAM_API_URL:-https://api.telegram.org}"

if [ -z "$SMTP_HOST" ] && [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo "LỖI: chưa đặt kênh gửi cảnh báo nào trong .env." >&2
  echo "     Telegram: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID. Mail: SMTP_HOST, SMTP_FROM, ALERT_EMAIL_TO. Xem README 7.14-7.15." >&2
  exit 1
fi

cp /etc/alertmanager/alertmanager.yml /tmp/alertmanager.yml

# ---- Telegram ----
if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
  : "${TELEGRAM_CHAT_ID:?Có TELEGRAM_BOT_TOKEN nhưng chưa đặt TELEGRAM_CHAT_ID trong .env}"
  # Token ghi ra file riêng trong container, không chèn vào cấu hình
  printf '%s' "$TELEGRAM_BOT_TOKEN" > /tmp/telegram_token
  sed -i -e "s#__TELEGRAM_CHAT_ID__#${TELEGRAM_CHAT_ID}#" \
         -e "s#__TELEGRAM_API_URL__#${TELEGRAM_API_URL}#" /tmp/alertmanager.yml
  # In độ dài token (không in token) để so với .env khi gỡ lỗi: token đúng thường dài 46
  echo "Kênh Telegram: BẬT (chat_id ${TELEGRAM_CHAT_ID}, token dài ${#TELEGRAM_BOT_TOKEN} ký tự)"
else
  sed -i '/# >>> telegram/,/# <<< telegram/d' /tmp/alertmanager.yml
  echo "Kênh Telegram: tắt (không có TELEGRAM_BOT_TOKEN)"
fi

# ---- Mail ----
if [ -n "$SMTP_HOST" ]; then
  : "${SMTP_FROM:?Có SMTP_HOST nhưng chưa đặt SMTP_FROM trong .env}"
  : "${ALERT_EMAIL_TO:?Có SMTP_HOST nhưng chưa đặt ALERT_EMAIL_TO trong .env}"
  # Mật khẩu ghi ra file riêng (chỉ nằm trong container), không chèn vào cấu hình
  # -> mật khẩu có ký tự đặc biệt cũng không làm hỏng file YAML
  printf '%s' "${SMTP_PASSWORD:-}" > /tmp/smtp_password
  sed -i -e "s#__SMTP_HOST__#${SMTP_HOST}#" \
         -e "s#__SMTP_FROM__#${SMTP_FROM}#" \
         -e "s#__SMTP_USERNAME__#${SMTP_USERNAME}#" \
         -e "s#__SMTP_REQUIRE_TLS__#${SMTP_REQUIRE_TLS}#" \
         -e "s#__ALERT_EMAIL_TO__#${ALERT_EMAIL_TO}#" /tmp/alertmanager.yml
  # Máy chủ mail không cần đăng nhập (ví dụ mailpit để test) -> bỏ 2 dòng auth
  if [ -z "$SMTP_USERNAME" ]; then
    sed -i '/smtp_auth_/d' /tmp/alertmanager.yml
  fi
  echo "Kênh mail: BẬT (gửi tới ${ALERT_EMAIL_TO})"
else
  sed -i '/# >>> email/,/# <<< email/d' /tmp/alertmanager.yml
  echo "Kênh mail: tắt (không có SMTP_HOST)"
fi

# ---- AI phân tích (holmes-bridge) ----
AI_WEBHOOK_URL=$(printf '%s' "${AI_WEBHOOK_URL:-}" | tr -d ' \r\n"'"'")
if [ -n "$AI_WEBHOOK_URL" ]; then
  sed -i "s#__AI_WEBHOOK_URL__#${AI_WEBHOOK_URL}#" /tmp/alertmanager.yml
  echo "AI phân tích: BẬT (webhook ${AI_WEBHOOK_URL})"
else
  sed -i '/# >>> ai/,/# <<< ai/d' /tmp/alertmanager.yml
  echo "AI phân tích: tắt (không có AI_WEBHOOK_URL)"
fi

# --cluster.listen-address= (để trống): chỉ chạy 1 bản, tắt chế độ cụm nhiều máy
exec /bin/alertmanager \
  --config.file=/tmp/alertmanager.yml \
  --storage.path=/alertmanager \
  --cluster.listen-address= \
  --web.external-url="${ALERTMANAGER_URL:-http://localhost:9093}" \
  --web.route-prefix=/
