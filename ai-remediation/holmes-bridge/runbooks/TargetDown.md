# TargetDown — Prometheus không lấy được số từ một target
Nhãn `job`/`instance` cho biết target nào. Hệ thống: backend, nginx(-exporter), mysql(-exporter), node-exporter, prometheus, alertmanager.

## Điều tra trước (chỉ đọc)
1. PromQL: `up == 0` -> liệt kê target chết. Nhiều target cùng chết một lúc? (nếu có: nghi mạng `ecommerce-shared` hoặc Prometheus)
2. LogQL: `{service="<job>", level="error"}` 15 phút gần nhất.
3. PromQL: `time() - process_start_time_seconds{job="backend"}` (vừa khởi động lại?).

## Nguyên nhân thường gặp
- Container dừng/crash (OOM, lỗi cấu hình) -> log cuối có stack trace hoặc "Killed".
- Backend không kết nối được DB nên healthcheck lỗi -> xem MySQLDown.
- Exporter chết nhưng app vẫn chạy (chỉ mất giám sát, khách không ảnh hưởng).

## Hành động có thể đề xuất (action_id)
- `restart_container` với đúng container của target (không phải `ecommerce-db`).
- Nhiều target chết cùng lúc hoặc là db: KHÔNG đề xuất restart, báo người vận hành.
- Muốn biết vì sao chết trước khi restart: `logs_tail` của đúng container đó (chỉ đọc; kết quả được đưa lại cho bạn ở vòng sau).

## Nếu lệnh trước không hiệu quả (vòng sau)
- Đã `restart_container` mà target vẫn `up == 0`: đọc `logs_tail` (nếu chưa), tìm lỗi cấu hình/OOM; đừng restart lại lần nữa (có cooldown 5 phút). Hết cách an toàn thì ghi `không có`.
