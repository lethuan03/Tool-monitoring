# HostHighMemory — RAM máy chủ trên 90%
Hết RAM thì Linux giết process (thường là MySQL).

## Điều tra trước (chỉ đọc)
1. PromQL: `100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)`.
2. RAM backend: `process_resident_memory_bytes{job="backend"}` có tăng mãi (rò rỉ)?
3. LogQL: `{service="db"} |= "Out of memory"`; số lần khởi động lại: `mysql_global_status_uptime`.

## Nguyên nhân thường gặp
- Backend rò RAM. Prometheus/Loki giữ nhiều dữ liệu. Máy quá nhỏ so với số container.

## Hành động có thể đề xuất (action_id)
- Backend rò RAM rõ ràng: `restart_container` -> `ecommerce-backend`.
- Còn lại: chẩn đoán, KHÔNG restart db.
