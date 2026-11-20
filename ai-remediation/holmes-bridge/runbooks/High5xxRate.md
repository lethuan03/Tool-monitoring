# High5xxRate — hơn 5% request của khách bị lỗi 5xx

## Điều tra trước (chỉ đọc)
1. PromQL: `sum by (route, status_code) (rate(http_requests_total{route!~"/health|/metrics",status_code=~"5.."}[5m]))` — route nào lỗi?
2. LogQL: `{service="backend", level="error"}` — xem `err_code`/`err_message` (ENOTFOUND db, ECONNREFUSED, ER_CON_COUNT_ERROR...).
3. PromQL: `mysql_up`, `up{job="backend"}`.

## Nguyên nhân thường gặp
- MySQL chết/không phân giải được tên `db` -> xem MySQLDown.
- Hết kết nối MySQL. Lỗi code ở một route sau khi cập nhật image. Backend quá tải (CPU/event loop cao).

## Hành động có thể đề xuất (action_id)
- Gốc là MySQL: chẩn đoán, KHÔNG restart db.
- Backend treo/rò RAM (RAM tăng mãi, event loop lag cao): `restart_container` -> `ecommerce-backend`.
- Lỗi bắt đầu ngay sau lần cập nhật image: nêu rõ cần người vận hành rollback.
