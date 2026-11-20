# HighLatencyP95 — 5% request chậm nhất mất hơn 1 giây

## Điều tra trước (chỉ đọc)
1. p95 theo route: `histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket{route!~"/health|/metrics"}[5m])))`.
2. MySQL: `rate(mysql_global_status_slow_queries[5m])`, `mysql_global_status_threads_connected`.
3. Máy chủ: CPU, RAM, `nodejs_eventloop_lag_seconds`.
4. LogQL: `{service="backend"} | json | duration_ms > 500`.

## Nguyên nhân thường gặp
- Query MySQL chậm / gần hết kết nối. Máy chủ quá tải. Event loop Node bị chặn. Lưu lượng tăng đột biến.

## Hành động có thể đề xuất (action_id)
- Thường chỉ chẩn đoán.
- Event loop lag cao kéo dài và RAM backend tăng mãi: `restart_container` -> `ecommerce-backend`.
- Hữu ích: `logs_tail` của backend.
