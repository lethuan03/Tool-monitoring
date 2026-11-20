# MySQLTooManyConnections — dùng quá 80% số kết nối tối đa

## Điều tra trước (chỉ đọc)
1. PromQL: `mysql_global_status_threads_connected`, `mysql_global_variables_max_connections`, `rate(mysql_global_status_aborted_connects[5m])`.
2. Lưu lượng có tăng đột biến? `sum(rate(http_requests_total[1m]))`.
3. LogQL: `{service="backend", level="error"} |= "ER_CON_COUNT_ERROR"`.

## Nguyên nhân thường gặp
- Lưu lượng tăng. Backend giữ kết nối không trả (rò rỉ pool) -> số kết nối chỉ tăng. Truy vấn treo.

## Hành động có thể đề xuất (action_id)
- Kết nối tăng mãi dù lưu lượng bình thường (nghi rò rỉ pool): `restart_container` -> `ecommerce-backend`.
- KHÔNG restart `ecommerce-db`.
